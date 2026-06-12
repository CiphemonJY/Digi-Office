#!/usr/bin/env python3
"""
Ciphemon Agent — Direct Coordinator Integration.

Polls coordinator for tasks using GET /tasks/claim.
"""

import json
import os
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import requests

# Configuration
COORDINATOR = "http://100.119.15.111:8080"
TOKEN = os.environ.get("DIGI_OFFICE_TOKEN", "8ecbedddd485c64eda2f49b7c1b78c800ddee8541eb92616a5f5a26c9ba217e1")
DB_PATH = "/Users/Ciphemon/.openclaw/workspace/LISA_FTM/db_523/ontology_mem_expanded.pkl"
ALPHA = 2.339


class SleepEngine:
    """Sleep consolidation engine."""
    
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


def handle_sleep_task(task_payload: dict) -> dict:
    """Process a sleep_consolidation task."""
    concept_code = task_payload.get("concept_code")
    n_passes = task_payload.get("n_passes", 4)
    
    if not concept_code or concept_code not in engine.codes:
        return {"status": "error", "error": f"Concept {concept_code} not found"}
    
    idx = engine.codes.index(concept_code)
    source_vec = engine.embeddings[idx].copy()
    sleep_vec = engine.sleep_consolidate(source_vec, n_passes=n_passes)
    
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


# Initialize engine
engine = SleepEngine(DB_PATH, ALPHA)
print(f"✓ SleepEngine loaded: {len(engine.codes)} concepts, alpha={ALPHA}")


def main():
    print("=" * 60)
    print("CIPHEMON AGENT — Direct Coordinator Mode")
    print("=" * 60)
    print(f"Coordinator: {COORDINATOR}")
    
    headers = {}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    
    # Test health
    try:
        resp = requests.get(f"{COORDINATOR}/health", timeout=5)
        health = resp.json()
        print(f"\n✓ Coordinator health: {health}")
    except Exception as e:
        print(f"\n⚠ Coordinator unreachable: {e}")
        return
    
    agent_id = "ciphemon"
    
    print(f"\n[READY] Agent '{agent_id}' starting poll loop...")
    print("Press Ctrl+C to stop\n")
    
    try:
        while True:
            # Send heartbeat
            try:
                hb_resp = requests.post(
                    f"{COORDINATOR}/agents/{agent_id}/heartbeat",
                    headers=headers,
                    timeout=10
                )
                if hb_resp.status_code == 200:
                    print(f"♥ Heartbeat OK", end="\r")
            except Exception as e:
                print(f"♥ Heartbeat failed: {e}")
            
            # Claim task
            try:
                claim_resp = requests.get(
                    f"{COORDINATOR}/tasks/claim",
                    headers=headers,
                    params={"agent_id": agent_id, "capabilities": "data_processing,validation"},
                    timeout=10
                )
                
                if claim_resp.status_code == 200:
                    task = claim_resp.json()
                    print(f"\n✓ Claimed task: {task.get('id')} ({task.get('type')})")
                    
                    # Process task
                    if task.get("type") == "sleep_consolidation":
                        result = handle_sleep_task(task.get("payload", {}))
                        
                        # Complete task
                        complete_resp = requests.post(
                            f"{COORDINATOR}/tasks/{task['id']}/complete",
                            headers={**headers, "Content-Type": "application/json"},
                            json={"result": result},
                            timeout=10
                        )
                        print(f"  → Completed: {complete_resp.status_code}")
                        print(f"  → Result: {json.dumps(result, indent=2)[:200]}")
                    else:
                        # Release unknown task type
                        requests.post(
                            f"{COORDINATOR}/tasks/{task['id']}/release",
                            headers=headers,
                            timeout=10
                        )
                        print(f"  → Released (unknown type)")
                
                elif claim_resp.status_code == 204:
                    print("· No tasks available", end="\r")
                
            except Exception as e:
                print(f"\n⚠ Claim failed: {e}")
            
            time.sleep(5)
    
    except KeyboardInterrupt:
        print("\n\n👋 Agent stopped.")


if __name__ == "__main__":
    main()
