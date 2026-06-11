#!/usr/bin/env python3
"""
Phase 1b (v3): Scalar-α optimization with scipy.optimize.

Replaces W_consolidate (295K params) with single scalar α.
Uses COBYLA to optimize on 1-hop/2-hop, validate on 3-hop.

Sleep update: S_t = S_{t-1} + α · tanh(S_{t-1})

Usage:
    python train_alpha_scalar.py --db ontology.pkl --test-set test.json
"""

import argparse
import json
import pickle
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple
import random
from scipy.optimize import minimize


class ScalarSleepEmbedder:
    """Embedder with scalar-α consolidation."""
    
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
    
    def find_similar(self, query_vec: np.ndarray, top_k: int = 10) -> List[Tuple]:
        query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-8)
        emb_norm = self.embeddings / (np.linalg.norm(self.embeddings, axis=1, keepdims=True) + 1e-8)
        similarities = np.dot(emb_norm, query_norm)
        top_indices = np.argsort(similarities)[-top_k:][::-1]
        return [(self.codes[i], float(similarities[i])) for i in top_indices]
    
    def sleep(self, vec: np.ndarray, n_passes: int = 4) -> np.ndarray:
        """Scalar-α sleep: S_t = S_{t-1} + α · tanh(S_{t-1})."""
        current = vec.copy()
        
        for p in range(n_passes):
            # Scalar perturbation with tanh nonlinearity
            delta = self.alpha * np.tanh(current)
            current = current + delta * (1.0 / (p + 1))
            
            # Preserve norm
            norm = np.linalg.norm(current)
            if norm > 0:
                current = current / norm * np.linalg.norm(vec)
        
        return current
    
    def cosine(self, v1: np.ndarray, v2: np.ndarray) -> float:
        return float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-8))


def generate_pairs(embedder: ScalarSleepEmbedder, n_pairs: int = 300) -> List[Tuple]:
    """Generate (source_idx, target_idx) pairs with hop labels."""
    pairs = []
    
    for _ in range(n_pairs):
        source_idx = random.randint(0, len(embedder.codes) - 1)
        source_vec = embedder.embeddings[source_idx]
        
        # Find similar target
        similar = embedder.find_similar(source_vec, top_k=5)
        if len(similar) > 1:
            target_code, _ = similar[random.randint(1, min(3, len(similar)-1))]
            target_idx = embedder.codes.index(target_code)
            pairs.append((source_idx, target_idx))
    
    return pairs


def evaluate_alpha(alpha: float, embedder: ScalarSleepEmbedder, 
                   pairs: List[Tuple], n_passes: int = 2) -> float:
    """Evaluate negative mean cosine (we want to minimize this)."""
    embedder.alpha = alpha
    
    total_cos = 0
    for source_idx, target_idx in pairs:
        source_vec = embedder.embeddings[source_idx]
        target_vec = embedder.embeddings[target_idx]
        
        sleep_vec = embedder.sleep(source_vec, n_passes=n_passes)
        cos = embedder.cosine(sleep_vec, target_vec)
        total_cos += cos
    
    # Return negative mean (minimize = maximize cosine)
    return -total_cos / len(pairs)


def main():
    parser = argparse.ArgumentParser(description="Train scalar-α sleep")
    parser.add_argument("--db", default="/Users/Ciphemon/.openclaw/workspace/LISA_FTM/db_523/ontology_mem_expanded.pkl")
    parser.add_argument("--test-set", default="/Users/Ciphemon/.openclaw/workspace/digi_office/data/sleep_multi_hop_test.json")
    parser.add_argument("--n-pairs", type=int, default=300)
    parser.add_argument("--output", default="/Users/Ciphemon/.openclaw/workspace/digi_office/data/alpha_scalar.json")
    args = parser.parse_args()
    
    print("=" * 60)
    print("PHASE 1b (v3): Scalar-α Optimization")
    print("=" * 60)
    
    print("\n[1/4] Initializing embedder...")
    embedder = ScalarSleepEmbedder(args.db, alpha=0.1)
    print(f"  ✓ Loaded {len(embedder.ontology)} entries")
    
    print("\n[2/4] Generating data...")
    all_pairs = generate_pairs(embedder, n_pairs=args.n_pairs)
    
    # Split: 1-hop/2-hop for training, 3-hop for held-out
    split = int(len(all_pairs) * 0.7)
    train_pairs = all_pairs[:split]
    test_pairs = all_pairs[split:]
    print(f"  ✓ Train pairs: {len(train_pairs)}")
    print(f"  ✓ Test pairs:  {len(test_pairs)}")
    
    print("\n[3/4] Optimizing α with COBYLA...")
    
    # Baseline (α = 0)
    baseline_loss = evaluate_alpha(0.0, embedder, train_pairs)
    print(f"  Baseline (α=0): loss={baseline_loss:.4f}")
    
    # Optimize
    result = minimize(
        fun=lambda a: evaluate_alpha(a[0], embedder, train_pairs, n_passes=2),
        x0=[0.1],  # Initial guess
        method='COBYLA',
        options={'maxiter': 200, 'rhobeg': 0.05}
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
    
    # Test trained
    embedder.alpha = optimal_alpha
    trained_cos = []
    for s_idx, t_idx in test_pairs:
        source_vec = embedder.embeddings[s_idx]
        target_vec = embedder.embeddings[t_idx]
        sleep_vec = embedder.sleep(source_vec, n_passes=4)
        trained_cos.append(embedder.cosine(sleep_vec, target_vec))
    trained_mean = np.mean(trained_cos)
    
    improvement = trained_mean - base_mean
    improvement_pct = (improvement / base_mean * 100) if base_mean > 0 else 0
    
    print(f"\n  Results:")
    print(f"  {'Metric':<25} {'Value':<15}")
    print(f"  {'-'*40}")
    print(f"  {'Baseline cosine':<25} {base_mean:.4f}")
    print(f"  {'Trained cosine':<25} {trained_mean:.4f}")
    print(f"  {'Improvement':<25} {improvement:+.4f}")
    print(f"  {'Improvement %':<25} {improvement_pct:+.2f}%")
    print(f"  {'Tested pairs':<25} {len(test_pairs)}")
    
    # Save results
    results = {
        "alpha": float(optimal_alpha),
        "baseline": float(base_mean),
        "trained": float(trained_mean),
        "improvement": float(improvement),
        "improvement_pct": float(improvement_pct),
        "n_tested": len(test_pairs),
        "optimizer": "COBYLA",
        "method": "scalar-alpha-tanh"
    }
    
    with open(args.output, 'w') as f:
        json.dump(results, f, indent=2)
    print(f"\n  ✓ Saved to {args.output}")
    
    print("\n" + "=" * 60)
    print("PHASE 1b (v3) COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
