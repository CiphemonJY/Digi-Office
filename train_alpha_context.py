#!/usr/bin/env python3
"""
Phase 1b (v4): Scalar-α with neighbor context.

Sleep update: S_t = S_{t-1} + α · mean(neighbor_contexts)

This is the actual mechanism from the paper — a scalar controls
how much neighbor context is blended into the current embedding.

Usage:
    python train_alpha_context.py --db ontology.pkl
"""

import argparse
import json
import pickle
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple
import random
from scipy.optimize import minimize


class ContextSleepEmbedder:
    """Embedder with scalar-α + neighbor context."""
    
    def __init__(self, ontology_path: str, alpha: float = 0.1):
        with open(ontology_path, 'rb') as f:
            self.ontology = pickle.load(f)
        
        self.codes = [entry[0] for entry in self.ontology]
        self.systems = [entry[1] for entry in self.ontology]
        self.displays = [entry[2] for entry in self.ontology]
        
        # Normalize embeddings to 384-dim
        self.embeddings = []
        for entry in self.ontology:
            vec = np.array(entry[3], dtype=np.float32)
            if len(vec) > 384:
                vec = vec[:384]
            elif len(vec) < 384:
                vec = np.pad(vec, (0, 384 - len(vec)), mode='constant')
            self.embeddings.append(vec)
        self.embeddings = np.array(self.embeddings)
        
        # Single scalar parameter
        self.alpha = alpha
        self.threshold = 0.6
        self.top_k = 5  # Number of neighbors to use
    
    def find_similar(self, query_vec: np.ndarray, top_k: int = 10) -> List[Tuple]:
        query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-8)
        emb_norm = self.embeddings / (np.linalg.norm(self.embeddings, axis=1, keepdims=True) + 1e-8)
        similarities = np.dot(emb_norm, query_norm)
        top_indices = np.argsort(similarities)[-top_k:][::-1]
        return [(self.codes[i], float(similarities[i])) for i in top_indices]
    
    def sleep(self, vec: np.ndarray, n_passes: int = 4) -> np.ndarray:
        """Sleep with neighbor context: S_t = S_{t-1} + α · mean(neighbors)."""
        current = vec.copy()
        
        for p in range(n_passes):
            # Find neighbors
            similar = self.find_similar(current, top_k=self.top_k + 1)  # +1 to skip self
            
            # Get neighbor vectors (above threshold)
            neighbor_vecs = []
            for code, sim in similar[1:]:  # Skip self
                if sim > self.threshold:
                    idx = self.codes.index(code)
                    neighbor_vecs.append(self.embeddings[idx])
            
            if not neighbor_vecs:
                break
            
            # Average neighbor context
            context = np.mean(neighbor_vecs, axis=0)
            
            # Scalar-α update
            delta = self.alpha * context
            current = current + delta * (1.0 / (p + 1))
            
            # Preserve norm
            norm = np.linalg.norm(current)
            if norm > 0:
                current = current / norm * np.linalg.norm(vec)
        
        return current
    
    def cosine(self, v1: np.ndarray, v2: np.ndarray) -> float:
        return float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-8))


def generate_pairs(embedder: ContextSleepEmbedder, n_pairs: int = 400) -> List[Tuple]:
    """Generate (source_idx, target_idx) pairs."""
    pairs = []
    
    for _ in range(n_pairs):
        source_idx = random.randint(0, len(embedder.codes) - 1)
        source_vec = embedder.embeddings[source_idx]
        
        # Find semantically similar target (different code)
        similar = embedder.find_similar(source_vec, top_k=10)
        valid_targets = [s for s in similar[1:] if s[1] > 0.5]  # threshold
        
        if valid_targets:
            target_code, _ = random.choice(valid_targets[:3])
            target_idx = embedder.codes.index(target_code)
            pairs.append((source_idx, target_idx))
    
    return pairs


def evaluate(alpha: float, embedder: ContextSleepEmbedder, 
             pairs: List[Tuple], n_passes: int = 2) -> float:
    """Return negative mean cosine (minimize this)."""
    embedder.alpha = alpha
    
    total_cos = 0
    for source_idx, target_idx in pairs:
        source_vec = embedder.embeddings[source_idx]
        target_vec = embedder.embeddings[target_idx]
        
        sleep_vec = embedder.sleep(source_vec, n_passes=n_passes)
        cos = embedder.cosine(sleep_vec, target_vec)
        total_cos += cos
    
    return -total_cos / len(pairs)


def main():
    parser = argparse.ArgumentParser(description="Train scalar-α with context")
    parser.add_argument("--db", default="/Users/Ciphemon/.openclaw/workspace/LISA_FTM/db_523/ontology_mem_expanded.pkl")
    parser.add_argument("--n-pairs", type=int, default=400)
    parser.add_argument("--output", default="/Users/Ciphemon/.openclaw/workspace/digi_office/data/alpha_context.json")
    args = parser.parse_args()
    
    print("=" * 60)
    print("PHASE 1b (v4): Scalar-α + Neighbor Context")
    print("=" * 60)
    
    print("\n[1/4] Initializing embedder...")
    embedder = ContextSleepEmbedder(args.db, alpha=0.1)
    print(f"  ✓ Loaded {len(embedder.ontology)} entries")
    
    print("\n[2/4] Generating data...")
    all_pairs = generate_pairs(embedder, n_pairs=args.n_pairs)
    
    # Split train/test
    split = int(len(all_pairs) * 0.75)
    train_pairs = all_pairs[:split]
    test_pairs = all_pairs[split:]
    print(f"  ✓ Train pairs: {len(train_pairs)}")
    print(f"  ✓ Test pairs:  {len(test_pairs)}")
    
    print("\n[3/4] Optimizing α with COBYLA...")
    
    # Baseline
    baseline_loss = evaluate(0.0, embedder, train_pairs, n_passes=2)
    print(f"  Baseline (α=0): loss={baseline_loss:.4f}")
    
    # Optimize α
    result = minimize(
        fun=lambda a: evaluate(a[0], embedder, train_pairs, n_passes=2),
        x0=[0.1],
        method='COBYLA',
        options={'maxiter': 300, 'rhobeg': 0.1}
    )
    
    optimal_alpha = result.x[0]
    print(f"  Optimal α: {optimal_alpha:.6f}")
    print(f"  Training loss: {result.fun:.4f}")
    print(f"  Success: {result.success}")
    
    print("\n[4/4] Evaluating on held-out test...")
    
    # Test baseline
    embedder.alpha = 0.0
    baseline_cos = []
    for s_idx, t_idx in test_pairs:
        base_vec = embedder.embeddings[s_idx]
        target_vec = embedder.embeddings[t_idx]
        baseline_cos.append(embedder.cosine(base_vec, target_vec))
    base_mean = np.mean(baseline_cos)
    
    # Test trained (with more passes)
    embedder.alpha = optimal_alpha
    trained_cos_2pass = []
    trained_cos_4pass = []
    for s_idx, t_idx in test_pairs:
        source_vec = embedder.embeddings[s_idx]
        target_vec = embedder.embeddings[t_idx]
        
        sleep_2 = embedder.sleep(source_vec, n_passes=2)
        trained_cos_2pass.append(embedder.cosine(sleep_2, target_vec))
        
        sleep_4 = embedder.sleep(source_vec, n_passes=4)
        trained_cos_4pass.append(embedder.cosine(sleep_4, target_vec))
    
    trained_2mean = np.mean(trained_cos_2pass)
    trained_4mean = np.mean(trained_cos_4pass)
    
    improvement_2 = trained_2mean - base_mean
    improvement_4 = trained_4mean - base_mean
    
    print(f"\n  Results:")
    print(f"  {'Metric':<30} {'Value':<15}")
    print(f"  {'-'*45}")
    print(f"  {'Baseline cosine':<30} {base_mean:.4f}")
    print(f"  {'Trained (2-pass) cosine':<30} {trained_2mean:.4f}")
    print(f"  {'Trained (4-pass) cosine':<30} {trained_4mean:.4f}")
    print(f"  {'Improvement (2-pass)':<30} {improvement_2:+.4f}")
    print(f"  {'Improvement (4-pass)':<30} {improvement_4:+.4f}")
    print(f"  {'Improvement % (4-pass)':<30} {(improvement_4/base_mean*100):+.2f}%")
    print(f"  {'Tested pairs':<30} {len(test_pairs)}")
    
    # Save
    results = {
        "alpha": float(optimal_alpha),
        "baseline": float(base_mean),
        "trained_2pass": float(trained_2mean),
        "trained_4pass": float(trained_4mean),
        "improvement_2pass": float(improvement_2),
        "improvement_4pass": float(improvement_4),
        "improvement_pct_4pass": float(improvement_4/base_mean*100) if base_mean > 0 else 0,
        "n_tested": len(test_pairs),
        "optimizer": "COBYLA",
        "method": "scalar-alpha-context"
    }
    
    with open(args.output, 'w') as f:
        json.dump(results, f, indent=2)
    print(f"\n  ✓ Saved to {args.output}")
    
    print("\n" + "=" * 60)
    print("PHASE 1b (v4) COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
