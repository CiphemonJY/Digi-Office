#!/usr/bin/env python3
"""
Ciphemon Agent — Connected to Hermes Coordinator.

Handles sleep_consolidation tasks with real-time coordinator integration.
"""

import json
import os
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).parent / "src"))

# Try to import SDK
try:
    from digi_office.agent_sdk.agent import Agent
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
    # Minimal fallback
    class Agent:
        def __init__(self, agent_id, coordinator_url, capabilities, token=None):
            self.agent_id = agent_id
            self.coordinator_url = coordinator_url
            self.capabilities = capabilities
            self.token = token
            self.session = requests.Session()
            if token:
                self.session.headers.update({"Authorization": f"Bearer {token}"})
        
        def task_handler(self, name):
            def decorator(func):
                setattr(self, f"handle_{name}", func)
                return func
            return decorator
        
        def start(self):
            print(f"Agent {self.agent_id} started")
            print(f"Coordinator: {self.coordinator_url}")
            print(f"Capabilities: {self.capabilities}")
            # Keep alive
            while True:
                time.sleep(30)
        
        def run(self, poll_interval=5, register_retries=0):
            self.start()


# Configuration
COORDINATOR = "http://100.119.15.111:8080"
TOKEN = os.environ.get("DIGI_OFFICE_TOKEN", "8ecbedddd485c64eda2f49b7c1b78c800ddee8541eb92616a5f5a26c9ba217e1")
DB_PATH = "/Users/Ciphemon/.openclaw/workspace/LISA_FTM/db_523/ontology_mem_expanded.pkl"
ALPHA = 2.339  # From Phase 1b training


class SleepEngine:
    """Sleep consolidation engine with scalar-α."""
    
    def __init__(self, ontology_path: str, alpha: float = 2.339):
        with open(ontology_path, 'rb') as f:
            ontology = pickle.load(f)
        
        self.codes = [entry[0] for entry in ontology]
        
        # Normalize embeddings
        self.embeddings = []
        for entry in ontology:
            vec = np.array(entry[3], dtype=np.float32)
            if len(vec) > 384:
                vec = vec[:384]
            elif len(vec) < 384:
                vec = np.pad(vec, (0, 384 - len(vec)), mode='constant')
            self.embeddings.append(vec)
        self.embeddings = np.array(self.embeddings)
        
        self.alpha = alpha
        self.threshold = 0.6
        self.top_k = 5
    
    def find_similar(self, query_vec: np.ndarray, top_k: int = 10):
        query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-8)
        emb_norm = self.embeddings / (np.linalg.norm(self.embeddings, axis=1, keepdims=True) + 1e-8)
        similarities = np.dot(emb_norm, query_norm)
        top_indices = np.argsort(similarities)[-top_k:][::-1]
        return [(self.codes[i], float(similarities[i])) for i in top_indices]
    
    def sleep_consolidate(self, vec: np.ndarray, n_passes: int = 4) -> np.ndarray:
        current = vec.copy()
        
        for p in range(n_passes):
            similar = self.find_similar(current, top_k=self.top_k + 1)
            neighbor_vecs = []
            for code, sim in similar[1:]:
                if sim > self.threshold:
                    idx = self.codes.index(code)
                    neighbor_vecs.append(self.embeddings[idx])
            
            if not neighbor_vecs:
                break
            
            context = np.mean(neighbor_vecs, axis=0)
            delta = self.alpha * context
            current = current + delta * (1.0 / (p + 1))
            
            norm = np.linalg.norm(current)
            if norm > 0:
                current = current / norm * np.linalg.norm(vec)
        
        return current


# Initialize engine
engine = SleepEngine(DB_PATH, ALPHA)
print(f"✓ SleepEngine loaded: {len(engine.codes)} concepts, alpha={ALPHA}")


# Create agent
agent = Agent(
    agent_id="ciphemon",
    coordinator_url=COORDINATOR,
    capabilities=["sleep_consolidation", "data_processing", "validation"],
    token=TOKEN
)


@agent.task_handler("sleep_consolidation")
def handle_sleep_consolidation(task):
    """Handle sleep_consolidation tasks."""
    payload = task.payload if hasattr(task, 'payload') else task.get("payload", {})
    
    concept_code = payload.get("concept_code")
    n_passes = payload.get("n_passes", 4)
    
    if not concept_code or concept_code not in engine.codes:
        return {
            "status": "error",
            "error": f"Concept {concept_code} not found"
        }
    
    # Run sleep consolidation
    idx = engine.codes.index(concept_code)
    source_vec = engine.embeddings[idx].copy()
    sleep_vec = engine.sleep_consolidate(source_vec, n_passes=n_passes)
    
    # Find neighbors
    neighbors = engine.find_similar(sleep_vec, top_k=5)
    
    return {
        "status": "success",
        "concept_code": concept_code,
        "n_passes": n_passes,
        "alpha": ALPHA,
        "neighbors": [
            {"code": code, "similarity": round(sim, 4)}
            for code, sim in neighbors[:5]
        ],
        "cosine_shift": round(
            float(np.dot(source_vec, sleep_vec) / 
                  (np.linalg.norm(source_vec) * np.linalg.norm(sleep_vec))), 4
        )
    }


def main():
    print("=" * 60)
    print("CIPHEMON AGENT — Coordinator Connection")
    print("=" * 60)
    print(f"Coordinator: {COORDINATOR}")
    print(f"Token: {TOKEN[:8]}...{TOKEN[-8:]}")
    print(f"Capabilities: {agent.capabilities}")
    
    # Test coordinator health
    try:
        resp = requests.get(f"{COORDINATOR}/health", timeout=5)
        print(f"\n✓ Coordinator health: {resp.status_code} — {resp.text[:50]}")
    except Exception as e:
        print(f"\n⚠ Coordinator unreachable: {e}")
        print("  Starting in standalone mode...")
    
    print("\n[READY] Agent starting...")
    
    # Try SDK run method, fallback to start
    if SDK_AVAILABLE:
        agent.run()
    else:
        agent.start()


if __name__ == "__main__":
    main()
