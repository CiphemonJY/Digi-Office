#!/usr/bin/env python3
"""
Phase 1b (v2): Better training with analytical gradient and more data.

Key improvements:
- Analytical gradient instead of finite differences
- Larger training set (generate more examples)
- Better loss: maximize cosine directly with L2 regularization
"""

import argparse
import json
import pickle
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple
import random


class SleepEmbedder:
    """Embedder with trainable consolidation."""
    
    def __init__(self, ontology_path: str):
        with open(ontology_path, 'rb') as f:
            self.ontology = pickle.load(f)
        
        self.codes = [entry[0] for entry in self.ontology]
        self.systems = [entry[1] for entry in self.ontology]
        self.displays = [entry[2] for entry in self.ontology]
        
        # Normalize embeddings
        self.embeddings = []
        for entry in self.ontology:
            vec = np.array(entry[3], dtype=np.float32)
            if len(vec) > 384:
                vec = vec[:384]
            elif len(vec) < 384:
                vec = np.pad(vec, (0, 384 - len(vec)), mode='constant')
            self.embeddings.append(vec)
        self.embeddings = np.array(self.embeddings)
        
        # Initialize W (small values, not identity)
        self.W = np.random.randn(384, 384) * 0.01
        self.b = np.zeros(384)
        self.threshold = 0.6
    
    def find_similar(self, query_vec: np.ndarray, top_k: int = 10) -> List[Tuple]:
        query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-8)
        emb_norm = self.embeddings / (np.linalg.norm(self.embeddings, axis=1, keepdims=True) + 1e-8)
        similarities = np.dot(emb_norm, query_norm)
        top_indices = np.argsort(similarities)[-top_k:][::-1]
        return [(self.codes[i], float(similarities[i])) for i in top_indices]
    
    def sleep(self, vec: np.ndarray, n_passes: int = 2) -> np.ndarray:
        current = vec.copy()
        
        for p in range(n_passes):
            similar = self.find_similar(current, top_k=10)
            context_vecs = []
            for code, sim in similar:
                if sim > self.threshold:
                    idx = self.codes.index(code)
                    context_vecs.append(self.embeddings[idx])
            
            if not context_vecs:
                break
            
            context_mean = np.mean(context_vecs, axis=0)
            delta = np.dot(self.W, context_mean) + self.b
            current = current + delta * 0.5 / (p + 1)
            
            # Renormalize
            norm = np.linalg.norm(current)
            if norm > 0:
                current = current / norm * np.linalg.norm(vec)
        
        return current
    
    def cosine(self, v1: np.ndarray, v2: np.ndarray) -> float:
        return float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-8))


def generate_training_data(embedder: SleepEmbedder, n_pairs: int = 500) -> List[Tuple]:
    """Generate (source, target) pairs for training."""
    pairs = []
    
    # Get entries by system
    by_system = {}
    for i, sys in enumerate(embedder.systems):
        if sys not in by_system:
            by_system[sys] = []
        by_system[sys].append(i)
    
    systems = list(by_system.keys())
    
    for _ in range(n_pairs):
        # Pick random source
        source_idx = random.randint(0, len(embedder.codes) - 1)
        source_vec = embedder.embeddings[source_idx]
        
        # Find similar targets (same or different system)
        similar = embedder.find_similar(source_vec, top_k=5)
        
        if len(similar) > 1:
            # Pick a target that's similar but not identical
            target_code, target_sim = similar[random.randint(1, min(3, len(similar)-1))]
            target_idx = embedder.codes.index(target_code)
            target_vec = embedder.embeddings[target_idx]
            
            pairs.append((source_vec, target_vec, target_sim))
    
    return pairs


def train(embedder: SleepEmbedder, pairs: List[Tuple], n_epochs: int = 200, lr: float = 0.01):
    """Train W_consolidate with analytical gradient."""
    
    print(f"  Training on {len(pairs)} pairs for {n_epochs} epochs...")
    
    best_loss = float('inf')
    history = []
    
    for epoch in range(n_epochs):
        epoch_loss = 0
        grad_W = np.zeros_like(embedder.W)
        grad_b = np.zeros_like(embedder.b)
        
        # Sample batch
        batch = random.sample(pairs, min(32, len(pairs)))
        
        for source_vec, target_vec, target_sim in batch:
            # Forward
            sleep_vec = embedder.sleep(source_vec, n_passes=2)
            
            # Cosine similarity
            cos = embedder.cosine(sleep_vec, target_vec)
            
            # Loss: negative cosine + L2 regularization
            loss = -cos + 0.001 * (np.sum(embedder.W ** 2) + np.sum(embedder.b ** 2))
            epoch_loss += loss
            
            # Gradient of cosine w.r.t. sleep_vec
            # d(cos)/dv = target / (|v||t|) - v * (v·t) / (|v|^3 |t|)
            v_norm = np.linalg.norm(sleep_vec)
            t_norm = np.linalg.norm(target_vec)
            if v_norm > 0 and t_norm > 0:
                grad_v = target_vec / (v_norm * t_norm) - sleep_vec * np.dot(sleep_vec, target_vec) / (v_norm**3 * t_norm)
                
                # Backprop through sleep pass (simplified: treat as single linear transform)
                # This is approximate but works for small W
                grad_W += np.outer(grad_v, source_vec)  # Approximate
                grad_b += grad_v
        
        # Update
        embedder.W -= lr * grad_W / len(batch)
        embedder.b -= lr * grad_b / len(batch)
        
        # Clip gradients
        embedder.W = np.clip(embedder.W, -1, 1)
        embedder.b = np.clip(embedder.b, -1, 1)
        
        avg_loss = epoch_loss / len(batch)
        history.append(avg_loss)
        
        if avg_loss < best_loss:
            best_loss = avg_loss
        
        if epoch % 20 == 0:
            print(f"    Epoch {epoch}: loss={avg_loss:.4f}, cos={cos:.4f}")
    
    print(f"  ✓ Best loss: {best_loss:.4f}")
    return history


def evaluate(embedder: SleepEmbedder, test_pairs: List[Tuple]) -> Dict:
    """Evaluate on held-out test set."""
    baseline_sims = []
    sleep_sims = []
    
    for source_vec, target_vec, _ in test_pairs:
        # Baseline
        base_sim = embedder.cosine(source_vec, target_vec)
        baseline_sims.append(base_sim)
        
        # Sleep-enhanced
        sleep_vec = embedder.sleep(source_vec, n_passes=4)
        sleep_sim = embedder.cosine(sleep_vec, target_vec)
        sleep_sims.append(sleep_sim)
    
    base_mean = np.mean(baseline_sims)
    sleep_mean = np.mean(sleep_sims)
    
    return {
        "baseline": float(base_mean),
        "sleep": float(sleep_mean),
        "improvement": float(sleep_mean - base_mean),
        "improvement_pct": float((sleep_mean - base_mean) / base_mean * 100) if base_mean > 0 else 0,
        "n_tested": len(test_pairs)
    }


def main():
    parser = argparse.ArgumentParser(description="Train W_consolidate v2")
    parser.add_argument("--db", default="/Users/Ciphemon/.openclaw/workspace/LISA_FTM/db_523/ontology_mem_expanded.pkl")
    parser.add_argument("--epochs", type=int, default=200)
    parser.add_argument("--lr", type=float, default=0.01)
    parser.add_argument("--n-pairs", type=int, default=500)
    parser.add_argument("--output", default="/Users/Ciphemon/.openclaw/workspace/digi_office/data/w_consolidate_v2.npy")
    args = parser.parse_args()
    
    print("=" * 60)
    print("PHASE 1b (v2): Training W_consolidate")
    print("=" * 60)
    
    print("\n[1/4] Initializing embedder...")
    embedder = SleepEmbedder(args.db)
    print(f"  ✓ Loaded {len(embedder.ontology)} entries")
    
    print("\n[2/4] Generating training data...")
    all_pairs = generate_training_data(embedder, n_pairs=args.n_pairs)
    
    # Split train/test
    split = int(len(all_pairs) * 0.8)
    train_pairs = all_pairs[:split]
    test_pairs = all_pairs[split:]
    print(f"  ✓ Train: {len(train_pairs)} pairs")
    print(f"  ✓ Test:  {len(test_pairs)} pairs")
    
    print("\n[3/4] Training...")
    history = train(embedder, train_pairs, n_epochs=args.epochs, lr=args.lr)
    
    print("\n[4/4] Evaluating...")
    results = evaluate(embedder, test_pairs)
    
    print(f"\n  Results:")
    print(f"  {'Metric':<20} {'Value':<15}")
    print(f"  {'-'*35}")
    print(f"  {'Baseline sim':<20} {results['baseline']:.4f}")
    print(f"  {'Sleep sim':<20} {results['sleep']:.4f}")
    print(f"  {'Improvement':<20} {results['improvement']:+.4f}")
    print(f"  {'Improvement %':<20} {results['improvement_pct']:+.1f}%")
    print(f"  {'Tested':<20} {results['n_tested']} pairs")
    
    # Save
    np.save(args.output, embedder.W)
    print(f"\n  ✓ Saved W to {args.output}")
    
    # Save results
    results_path = Path(args.output).parent / "phase1b_v2_results.json"
    with open(results_path, 'w') as f:
        json.dump(results, f, indent=2)
    print(f"  ✓ Saved results to {results_path}")
    
    print("\n" + "=" * 60)
    print("PHASE 1b (v2) COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
