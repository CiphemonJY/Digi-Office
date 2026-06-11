#!/usr/bin/env python3
"""
Phase 2: Coordinator Integration for LLM Sleep.

Registers with Hermes coordinator and handles sleep_consolidation tasks.
"""

import argparse
import json
import pickle
import numpy as np
import requests
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent / "src"))

# Agent SDK import
try:
    from digi_office.agent_sdk.agent import Agent as DigiOfficeAgent
    from digi_office.agent_sdk.agent import task_handler
except ImportError:
    # Fallback if SDK not available
    DigiOfficeAgent = object
    def task_handler(name):
        def decorator(func):
            return func
        return decorator


class SleepCoordinatorAgent:
    """Agent that performs sleep consolidation via coordinator tasks."""
    
    def __init__(self, agent_id: str = "ciphemon-sleep", ontology_path: str = None, alpha: float = 2.339):
        self.agent_id = agent_id
        
        # Load ontology
        if ontology_path:
            with open(ontology_path, 'rb') as f:
                self.ontology = pickle.load(f)
            
            self.codes = [entry[0] for entry in self.ontology]
            self.embeddings = []
            for entry in self.ontology:
                vec = np.array(entry[3], dtype=np.float32)
                if len(vec) > 384:
                    vec = vec[:384]
                elif len(vec) < 384:
                    vec = np.pad(vec, (0, 384 - len(vec)), mode='constant')
                self.embeddings.append(vec)
            self.embeddings = np.array(self.embeddings)
        else:
            self.ontology = []
            self.codes = []
            self.embeddings = np.array([])
        
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
    
    @task_handler("sleep_consolidation")
    def handle_sleep_task(self, task: Dict) -> Dict:
        """Handle sleep_consolidation tasks from coordinator."""
        
        payload = task.get("payload", {})
        concept_code = payload.get("concept_code")
        n_passes = payload.get("n_passes", 4)
        return_context = payload.get("return_context", True)
        
        if not concept_code or concept_code not in self.codes:
            return {
                "status": "error",
                "error": f"Concept {concept_code} not found in ontology"
            }
        
        # Get source vector
        idx = self.codes.index(concept_code)
        source_vec = self.embeddings[idx].copy()
        
        # Perform sleep consolidation
        sleep_vec = self.sleep_consolidate(source_vec, n_passes=n_passes)
        
        # Find post-sleep neighbors
        neighbors = self.find_similar(sleep_vec, top_k=5)
        
        result = {
            "status": "success",
            "concept_code": concept_code,
            "n_passes": n_passes,
            "alpha": self.alpha,
        }
        
        if return_context:
            result["neighbors"] = [
                {"code": code, "similarity": round(sim, 4)}
                for code, sim in neighbors[:5]
            ]
            result["cosine_shift"] = round(
                float(np.dot(source_vec, sleep_vec) / 
                      (np.linalg.norm(source_vec) * np.linalg.norm(sleep_vec))), 4
            )
        
        return result


def main():
    parser = argparse.ArgumentParser(description="Sleep Coordinator Agent")
    parser.add_argument("--db", default="/Users/Ciphemon/.openclaw/workspace/LISA_FTM/db_523/ontology_mem_expanded.pkl")
    parser.add_argument("--alpha", type=float, default=2.339)
    parser.add_argument("--test", action="store_true", help="Test without coordinator")
    args = parser.parse_args()
    
    print("=" * 60)
    print("PHASE 2: Sleep Coordinator Agent")
    print("=" * 60)
    
    agent = SleepCoordinatorAgent(
        agent_id="ciphemon-sleep",
        ontology_path=args.db,
        alpha=args.alpha
    )
    
    print(f"\n✓ Agent initialized")
    print(f"  Ontology: {len(agent.ontology)} entries")
    print(f"  Alpha: {agent.alpha}")
    
    if args.test:
        print("\n[TEST MODE] Running sample task...")
        test_task = {
            "id": "test-001",
            "type": "sleep_consolidation",
            "payload": {
                "concept_code": "44054006",  # Type 2 diabetes mellitus
                "n_passes": 4,
                "return_context": True
            }
        }
        result = agent.handle_sleep_task(test_task)
        print(f"\nResult: {json.dumps(result, indent=2)}")
    else:
        print("\n[RUN MODE] Connecting to coordinator...")
        # TODO: Connect to coordinator when available
        print("  Waiting for coordinator at 100.113.198.30:8080...")
        print("  (Coordinator currently unreachable)")
    
    print("\n" + "=" * 60)
    print("PHASE 2 AGENT READY")
    print("=" * 60)


if __name__ == "__main__":
    main()
