import os
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
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer
import requests
# --- Load local .env (for local testing) ---
load_dotenv()  # only affects local environment, ignored on Streamlit Cloud

# --- Secret helper function ---
def get_secret(key):
    """Retrieve secret from Streamlit Cloud, else fallback to local env."""
    if key in st.secrets:
        return st.secrets[key]
    return os.getenv(key)

# --- Retrieve API keys ---
api_key = get_secret("GOOGLE_API_KEY")
Qdrant_api_key = get_secret("QDRANT_API_KEY")

# --- Configure Google Generative AI ---
genai.configure(api_key=api_key)


# Initialize Qdrant Client (Global)
qdrant_client = QdrantClient(
    url="https://1ce4d383-d1a4-4135-8412-25a9813396c8.europe-west3-0.gcp.cloud.qdrant.io", 
    api_key=Qdrant_api_key
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("EV-Square-Bot")

st.set_page_config(page_title="EV Square AI Consultant", page_icon="⚡", layout="centered")

# --- STYLE CUSTOMIZATION ---
# --- STYLE CUSTOMIZATION ---
st.markdown("""
    <style>
    /* 1. Sets the background for the entire app */
    .stApp {
        background-color: #061a0c;
    }

    /* 2. Fixes the Header/Toolbar transparency */
    header, [data-testid="stHeader"] {
        background-color: rgba(0,0,0,0) !important;
    }

    /* 3. Sets Sidebar color to match the deep dark green */
    [data-testid="stSidebar"] {
        background-color: #0b2212;
    }

    /* 4. Styles Chat Messages bubbles */
    [data-testid="stChatMessage"] {
        background-color: #112d1a; 
        border-radius: 10px;
        margin-bottom: 10px;
        border: 1px solid #1a3d25;
    }

    /* 5. THE FIX: Remove the black background from the chat input container */
    [data-testid="stChatInputContainer"] {
        background-color: rgba(0, 0, 0, 0) !important;
        border: none !important;
    }

    /* 6. Style the actual input box to match your dark theme */
    [data-testid="stChatInput"] {
        background-color: #112d1a !important;
        border: 1px solid #1a3d25 !important;
        border-radius: 10px !important;
    }

    /* 7. Text color and caret color for the input field */
    [data-testid="stChatInput"] textarea {
        color: #e0e0e0 !important;
        caret-color: #e0e0e0 !important;
    }
    
    /* 8. Text color fix for general readability */
    .stMarkdown p {
        color: #e0e0e0;
    }
    </style>
    """, unsafe_allow_html=True)
# --- 1. INITIALIZE MODELS (Cached for Speed) ---
@st.cache_resource
def load_resources():
    genai.configure(api_key=api_key)
    llm = genai.GenerativeModel(
        model_name="gemini-2.0-flash", 
        system_instruction=SYSTEM_PROMPT
    )
    # ADD THIS: Load the local model to match your 384-dim Qdrant vectors
    embed_model = SentenceTransformer("all-MiniLM-L6-v2")
    return llm, embed_model

llm_model, embed_model = load_resources() # Update this line too



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
    embeddings = torch.tensor(vectors_list).float()
    
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
    # REMOVE the genai.embed_content block
    # ADD THIS: Use the same local model used for uploading
    q_emb_numpy = embed_model.encode(query)
    q_emb = torch.tensor(q_emb_numpy).float()
    
    # Ensure chunk_embeddings is also float for torch.mm compatibility
    global chunk_embeddings
    chunk_embeddings = chunk_embeddings.float()

    # 1. Dense Score (Semantic)
    # This will now be 384 vs 384 -> Success!
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

# Display Chat History
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])



def handle_chat_request(history):
    # 1. Map Streamlit roles to Gemini roles & format parts
    formatted_history = []
    for msg in history[:-1]:  # All previous turns
        role = "model" if msg["role"] == "assistant" else "user"
        formatted_history.append({
            "role": role,
            "parts": [{"text": msg["content"]}]
        })

    # 2. Get current query
    user_query = history[-1]["content"]
    
    # 3. Retrieval & Guardrail
    context, is_confident = get_hybrid_context(user_query)

    if not is_confident:
        full_prompt = (
            "The user's question is not covered in our technical documents. "
            "Strictly inform them you don't have information and offer support."
            f"\nUSER QUESTION: {user_query}"
        )
    else:
        # We inject context ONLY into the final message sent to the model
        full_prompt = f"CONTEXT FROM DOCUMENTS:\n{context}\n\nUSER QUESTION: {user_query}"

    # 4. Stateless Chat Call
    # Use the formatted history (model/user roles)
    temp_chat = llm_model.start_chat(history=formatted_history) 
    
    # Send the augmented prompt
    response_stream = temp_chat.send_message(full_prompt, stream=True)
    
    return response_stream



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

        # 2. Add User Message to UI State
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        # 3. Assistant Generation (Stateless)
        with st.chat_message("assistant"):
            try:
                # Call the stateless handler with the full history
                # This function now handles RAG internally
                response_stream = handle_chat_request(st.session_state.messages)
                
                def stream_generator():
                    for chunk in response_stream:
                        if chunk.text:
                            yield chunk.text

                # Stream to UI and capture the full text
                full_response = st.write_stream(stream_generator())
                
                # 4. Final Metrics & History Update
                latency = round(time.time() - start_time, 2)
                logger.info(f"Response completed in {latency}s")
                
                # Update UI history state
                st.session_state.messages.append({"role": "assistant", "content": full_response})
                
            except Exception as e:
                st.error(f"Error: {e}")
                logger.error(f"Chat Error: {str(e)}")

# Sidebar Info
with st.sidebar:
    st.header("System Status")
    if st.button("Clear Chat History"):
        st.session_state.messages = []
        # st.session_state.chat_session = llm_model.start_chat(history=[])
        st.rerun()
