import streamlit as st
import google.generativeai as genai
from pathlib import Path
from sentence_transformers import util
from rank_bm25 import BM25Okapi
import numpy as np
import torch
from prompt import SYSTEM_PROMPT
import logging
import sys
import time
from qdrant_client import QdrantClient

# --- INITIAL SETUP & LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("EV-Square-Bot")

st.set_page_config(page_title="EV Square AI Consultant", page_icon="⚡", layout="centered")

# --- STYLE CUSTOMIZATION ---
st.markdown("""
    <style>
    .main { background-color: #f5f7f9; }
    .stChatMessage { border-radius: 15px; margin-bottom: 10px; }
    </style>
    """, unsafe_allow_html=True)

# --- 1. INITIALIZE MODELS (Cached for Speed) ---
@st.cache_resource
def load_resources():
    # Use your existing Gemini API Key
    genai.configure(api_key="AIzaSyD605ZNDD7-nZdA4fWKxjHnr3SOwHVzdWQ")
    
    llm = genai.GenerativeModel(
        model_name="gemini-2.0-flash", 
        system_instruction=SYSTEM_PROMPT
    )
    # We remove SentenceTransformer local model because we'll use 
    # Gemini Embeddings to match your 1536-dim Qdrant vectors.
    return llm

llm_model = load_resources()

# Initialize Qdrant Client (Global)
qdrant_client = QdrantClient(
    url="https://1ce4d383-d1a4-4135-8412-25a9813396c8.europe-west3-0.gcp.cloud.qdrant.io", 
    api_key="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhY2Nlc3MiOiJtIiwic3ViamVjdCI6ImFwaS1rZXk6YWQwZjkyNGMtOGFhNi00YjM1LTgxMjMtMTQ1YTA2YTQ1MjBiIn0.1N1TYCApAZtQQQUidEah7o6k2BMEY_2juKgY3f7-IhU"
)

# --- 2. DATA LOADING & HYBRID INDEXING ---
@st.cache_data
def load_knowledge_base():
    COLLECTION_NAME = "EV_SQUARE_COLLECTION"
    
    # 1. Fetch all points from Qdrant
    response, _ = qdrant_client.scroll(
        collection_name=COLLECTION_NAME,
        limit=1000, 
        with_payload=True,
        with_vectors=True
    )
    
    # 2. Sort by ID to ensure order is maintained
    sorted_points = sorted(response, key=lambda x: x.id)
    
    # 3. Extract texts and vectors
    texts = [point.payload["text"] for point in sorted_points]
    vectors_list = [point.vector for point in sorted_points]
    
    # 4. Convert vectors to Torch Tensor (Maintains dimension compatibility)
    embeddings = torch.tensor(vectors_list) 
    
    # 5. Build Sparse Index (BM25) locally from retrieved texts
    tokenized_corpus = [doc.lower().split() for doc in texts]
    bm25 = BM25Okapi(tokenized_corpus)
    
    return texts, embeddings, bm25

chunk_texts, chunk_embeddings, bm25_index = load_knowledge_base()

# --- 3. SESSION STATE MANAGEMENT ---
if "messages" not in st.session_state:
    st.session_state.messages = []

if "chat_session" not in st.session_state:
    st.session_state.chat_session = llm_model.start_chat(history=[])

# --- 4. HYBRID RETRIEVAL FUNCTION ---
def get_hybrid_context(query, k=2):
    # USE THIS EXACT STRING FROM YOUR LIST_MODELS OUTPUT
    result = genai.embed_content(
        model="models/gemini-embedding-001",
        content=query,
        task_type="retrieval_query",
        output_dimensionality=1536
    )
    # Convert to tensor for your existing cos_sim logic
    q_emb = torch.tensor(result['embedding'])
    
    # 1. Dense Score (Semantic)
    dense_scores = util.cos_sim(q_emb, chunk_embeddings)[0].tolist()
    
    # --- GUARDRAIL: Check Max Similarity ---
    max_similarity = max(dense_scores) if dense_scores else 0
    is_confident = max_similarity > 0.4 
    # ---------------------------------------

    dense_rank = np.argsort(dense_scores)[::-1]
    
    # SPARSE SEARCH: Keyword Match
    sparse_scores = bm25_index.get_scores(query.lower().split())
    sparse_rank = np.argsort(sparse_scores)[::-1]
    
    # RRF Fusion (Your exact logic)
    fused = {}
    for r, idx in enumerate(dense_rank): 
        fused[idx] = fused.get(idx, 0) + 1/(60+r)
    for r, idx in enumerate(sparse_rank): 
        fused[idx] = fused.get(idx, 0) + 1/(60+r)
    
    top_ids = sorted(fused, key=fused.get, reverse=True)[:k]
    context = "\n".join([f"--- Source ---\n{chunk_texts[i]}" for i in top_ids])
    
    return context, is_confident

# --- 5. THE UI LAYOUT ---
st.title("⚡ EV Square AI Consultant")
st.caption("Environment: Hybrid RAG + Gemini 2.0 Flash + Qdrant Cloud")

# Display Chat History
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# --- CHAT INPUT & LOGIC ---
if prompt := st.chat_input("Ask about EV Square..."):
    start_time = time.time()

    # 1. Rate Limit Check
    if "last_request_time" not in st.session_state:
        st.session_state.last_request_time = 0
    
    RATE_LIMIT_SECONDS = 10
    current_time = time.time()
    time_since_last = current_time - st.session_state.last_request_time
    
    if time_since_last < RATE_LIMIT_SECONDS:
        wait_time = int(RATE_LIMIT_SECONDS - time_since_last)
        st.warning(f"⏳ Slow down! Please wait {wait_time} seconds.")
    else:
        st.session_state.last_request_time = current_time

        # 2. Add User Message to UI
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        # 3. Retrieval & Guardrail
        context, is_confident = get_hybrid_context(prompt)

        if not is_confident:
            full_prompt = (
                "The user's question is not covered in our technical documents. "
                "Strictly inform the user that you don't have information on this specific topic "
                "and offer to connect them to our support team. "
                f"USER QUESTION: {prompt}"
            )
            logger.warning(f"Guardrail Tripped for: {prompt}")
        else:
            full_prompt = f"CONTEXT:\n{context}\n\nUSER QUESTION: {prompt}"
            logger.info(f"RAG Match Found (Confident)")

        # 4. Assistant Generation (Streaming)
        with st.chat_message("assistant"):
            try:
                response_stream = st.session_state.chat_session.send_message(
                    full_prompt, 
                    stream=True
                )
                
                def stream_generator():
                    for chunk in response_stream:
                        if chunk.text:
                            yield chunk.text

                # Stream to UI
                full_response = st.write_stream(stream_generator())
                
                # Metrics & Final History Update
                latency = round(time.time() - start_time, 2)
                logger.info(f"Response completed in {latency}s")
                st.session_state.messages.append({"role": "assistant", "content": full_response})
                
            except Exception as e:
                st.error(f"Error: {e}")

# Sidebar Info
with st.sidebar:
    st.header("System Status")
    st.success("Connected to Gemini API")
    st.info(f"Knowledge Base: {len(chunk_texts)} Chunks Loaded")
    if st.button("Clear Chat History"):
        st.session_state.messages = []
        st.session_state.chat_session = llm_model.start_chat(history=[])
        st.rerun()