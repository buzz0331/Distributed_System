from app.celery_app import celery_app
import redis
import json
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import os

redis_client = redis.Redis(host='localhost', port=6379, db=3)

def delete_txt_file(txt_path):
    """txt 파일 삭제"""
    try:
        if os.path.exists(txt_path):
            os.remove(txt_path)
    except Exception as e:
        print(f"파일 삭제 중 에러 발생: {str(e)}")

# TF-IDF 코사인 유사도 계산
def process_paper_batch(papers_batch, meeting_text):
    # 1. 회의록과 논문들의 텍스트를 하나의 리스트로 합침
    # - 첫 번째 요소는 회의록, 나머지는 논문들의 텍스트
    # - 예: ["회의록 내용", "논문1 내용", "논문2 내용", ...]
    texts = [meeting_text] + [p['text_content'] for p in papers_batch]

    # 2. TF-IDF 벡터화 수행
    # - TF(단어 빈도): 특정 단어가 문서에 얼마나 자주 등장하는지
    # - IDF(역문서 빈도): 전체 문서에서 해당 단어가 얼마나 희소한지
    # - stop_words='english': 'the', 'is' 같은 불용어 제거
    # - 각 문서를 고차원 벡터로 변환 (단어 수만큼의 차원)
    vectorizer = TfidfVectorizer(stop_words='english')
    # - 각 문서를 고차원 벡터로 변환 (단어 수만큼의 차원)
    tfidf_matrix = vectorizer.fit_transform(texts)

    # 3. 코사인 유사도 계산
    # - tfidf_matrix[0:1]: 회의록의 TF-IDF 벡터
    # - tfidf_matrix[1:]: 논문들의 TF-IDF 벡터
    # - 결과: 회의록과 각 논문 간의 유사도 점수 (0~1 사이 값)
    # - 1에 가까울수록 더 유사함을 의미
    similarities = cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:])[0]
    return similarities

def chunk_text(text, chunk_size=2000):
    """텍스트를 청크 단위로 분할"""
    return [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]

@celery_app.task(name='workers.relevance_worker.check_and_select', bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 1, 'countdown': 5}, time_limit=300, soft_time_limit=290)
def check_and_select(self, meeting_id, meeting_text):
    # Redis에서 논문 리스트 가져오기
    paper_jsons = redis_client.lrange(f"relevance:{meeting_id}:papers", 0, -1)
    if len(paper_jsons) < 5:  # 5개 미만이면 대기
        return

    # 누적된 논문 가져오기
    accumulated_papers = []
    accumulated_key = f"relevance:{meeting_id}:accumulated"
    accumulated_jsons = redis_client.lrange(accumulated_key, 0, -1)
    if accumulated_jsons:
        accumulated_papers = [json.loads(p) for p in accumulated_jsons]

    # 현재 배치의 논문
    current_papers = [json.loads(p) for p in paper_jsons]
    
    # 모든 논문 합치기 (누적 + 현재)
    all_papers = accumulated_papers + current_papers
    
    # 병렬 처리를 위한 배치 분할
    batch_size = 2  # 각 스레드가 처리할 논문 수
    # 논문 배치 생성
    paper_batches = [all_papers[i:i + batch_size] for i in range(0, len(all_papers), batch_size)]
    
    # ThreadPoolExecutor로 병렬 처리
    with ThreadPoolExecutor(max_workers=3) as executor:
        # 각 배치에 대해 유사도 계산
        batch_similarities = list(executor.map(
            lambda batch: process_paper_batch(batch, meeting_text),
            paper_batches
        ))
    
    # 모든 배치의 유사도 결과 병합
    all_similarities = np.concatenate(batch_similarities)
    
    # 상위 3개 논문 선택
    top_indices = all_similarities.argsort()[::-1][:3]
    
    # 선택된 논문과 누적될 논문 분리
    selected_papers = []
    remaining_papers = []
    
    for i, paper in enumerate(all_papers):
        if i in top_indices:
            selected_papers.append(paper)
        else:
            remaining_papers.append(paper)
    
    # 선택된 논문을 llm_worker에 전송하고 역색인 처리
    for paper in selected_papers:
        # llm_worker에 전송
        celery_app.send_task(
            'workers.llm_worker.summarize_paper',
            args=[paper['title'], paper['meeting_id'], paper['txt_path'], paper.get('pdf_url', '')]
        )
        
        # 역색인 처리
        chunks = chunk_text(paper['text_content'], 2000)
        for chunk in chunks:
            celery_app.send_task(
                'workers.invertedindex_worker.build_inverted_index',
                args=[chunk, meeting_id]
            )
    
    # 처리된 논문 제거
    for paper_json in paper_jsons:
        redis_client.lrem(f"relevance:{meeting_id}:papers", 0, paper_json)
    
    # 누적 논문 업데이트
    redis_client.delete(accumulated_key)  # 기존 누적 논문 삭제
    if remaining_papers:  # 남은 논문이 있으면 누적
        for paper in remaining_papers:
            redis_client.rpush(accumulated_key, json.dumps(paper))
    
    # 남은 논문이 5개 이상이면 다시 처리
    if len(redis_client.lrange(f"relevance:{meeting_id}:papers", 0, -1)) >= 5:
        celery_app.send_task(
            'workers.relevance_worker.check_and_select',
            args=[meeting_id, meeting_text]
        )
    else:
        # 20개의 논문이 모두 처리되었을 때
        # 누적된 논문들의 txt 파일 삭제
        accumulated_jsons = redis_client.lrange(accumulated_key, 0, -1)
        if accumulated_jsons:
            for paper_json in accumulated_jsons:
                paper = json.loads(paper_json)
                if 'txt_path' in paper:
                    delete_txt_file(paper['txt_path'])
            # 누적된 논문 리스트도 삭제
            redis_client.delete(accumulated_key)
