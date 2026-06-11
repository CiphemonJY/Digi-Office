#!/usr/bin/env python3
"""
Phase 1b: Train W_consolidate on multi-hop ground truth.

Splits test set by hop depth:
- Train: 1-hop and 2-hop tasks → optimize W_consolidate
- Test: 3-hop (held-out) → measure generalization

Usage:
    python train_w_consolidate.py --db /path/to/ontology.pkl --test-set data/sleep_multi_hop_test.json
"""

import argparse
import json
import pickle
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple
import random


class Trainer:
    def __init__(self, ontology_path: str, test_set_path: str):
        # Load ontology
        with open(ontology_path, 'rb') as f:
            self.ontology = pickle.load(f)
        
        self.code_to_entry = {entry[0]: entry for entry in self.ontology}
        self.codes = [entry[0] for entry in self.ontology]
        
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
        
        # Load test set
        with open(test_set_path) as f:
            self.test_set = json.load(f)
        
        # Initialize W_consolidate (small random weights, not identity)
        self.W = np.random.randn(384, 384) * 0.01
        self.lr = 0.001
        self.threshold = 0.6
        
    def find_similar(self, query_vec: np.ndarray, top_k: int = 10) -> List[Tuple]:
        """Find similar entries."""
        query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-8)
        emb_norm = self.embeddings / (np.linalg.norm(self.embeddings, axis=1, keepdims=True) + 1e-8)
        similarities = np.dot(emb_norm, query_norm)
        top_indices = np.argsort(similarities)[-top_k:][::-1]
        return [(self.codes[i], float(similarities[i])) for i in top_indices]
    
    def compute_similarity(self, v1: np.ndarray, v2: np.ndarray) -> float:
        return float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-8))
    
    def sleep_pass(self, vec: np.ndarray, target_vec: np.ndarray, n_passes: int = 2) -> np.ndarray:
        """Run sleep consolidation with current W."""
        current = vec.copy()
        
        for p in range(n_passes):
            # Find context
            similar = self.find_similar(current, top_k=10)
            context_vecs = []
            for code, sim in similar:
                if sim > self.threshold:
                    idx = self.codes.index(code)
                    context_vecs.append(self.embeddings[idx].copy())
            
            if not context_vecs:
                break
            
            context_mean = np.mean(context_vecs, axis=0)
            delta = np.dot(self.W, context_mean)
            current = current + delta * (1.0 / (p + 1))
            current = current / (np.linalg.norm(current) + 1e-8) * np.linalg.norm(vec)
        
        return current
    
    def train_step(self, source_vec: np.ndarray, target_vec: np.ndarray, 
                   n_passes: int = 2) -> float:
        """One gradient step on W_consolidate."""
        # Forward: sleep consolidation
        sleep_vec = self.sleep_pass(source_vec, target_vec, n_passes)
        
        # Loss: negative cosine similarity (we want to maximize)
        cos_sim = self.compute_similarity(sleep_vec, target_vec)
        loss = 1.0 - cos_sim  # Minimize this
        
        # Simple finite-difference gradient for W
        # (In production, use autograd; here we use simple perturbation)
        grad = np.zeros_like(self.W)
        eps = 0.001
        
        # Sample a subset of W for gradient estimation (efficiency)
        for _ in range(10):
            i, j = random.randint(0, 383), random.randint(0, 383)
            self.W[i, j] += eps
            sleep_plus = self.sleep_pass(source_vec, target_vec, n_passes)
            loss_plus = 1.0 - self.compute_similarity(sleep_plus, target_vec)
            self.W[i, j] -= eps
            
            grad[i, j] = (loss_plus - loss) / eps
        
        # Update
        self.W -= self.lr * grad
        
        # Regularization: keep weights small
        self.W *= 0.999
        
        return loss
    
    def train(self, n_epochs: int = 100):
        """Train on 1-hop and 2-hop tasks."""
        
        # Split by hop depth
        train_tasks = [ex for ex in self.test_set 
                      if any(k in ex for k in ['hop_1', 'hop_2']) and 'hop_3' not in ex]
        held_out_3hop = [ex for ex in self.test_set if 'hop_3' in ex]
        
        print(f"  Train tasks (1-2 hop): {len(train_tasks)}")
        print(f"  Held-out (3-hop): {len(held_out_3hop)}")
        
        if len(train_tasks) == 0:
            print("  WARNING: No 1-2 hop tasks found. Using all tasks for training.")
            train_tasks = [ex for ex in self.test_set if 'hop_1' in ex]
        
        # Training loop
        print(f"\n  Training for {n_epochs} epochs...")
        best_loss = float('inf')
        
        for epoch in range(n_epochs):
            epoch_loss = 0
            
            for example in train_tasks:
                # Get source and target vectors
                source_idx = self.codes.index(example["source"]["code"])
                source_vec = self.embeddings[source_idx].copy()
                
                # Train on 1-hop target
                if "hop_1" in example:
                    target_idx = self.codes.index(example["hop_1"]["code"])
                    target_vec = self.embeddings[target_idx].copy()
                    loss = self.train_step(source_vec, target_vec, n_passes=2)
                    epoch_loss += loss
                
                # Train on 2-hop target
                if "hop_2" in example:
                    # Start from hop_1 position
                    hop1_idx = self.codes.index(example["hop_1"]["code"])
                    hop1_vec = self.embeddings[hop1_idx].copy()
                    target_idx = self.codes.index(example["hop_2"]["code"])
                    target_vec = self.embeddings[target_idx].copy()
                    loss = self.train_step(hop1_vec, target_vec, n_passes=2)
                    epoch_loss += loss
            
            avg_loss = epoch_loss / max(len(train_tasks), 1)
            
            if avg_loss < best_loss:
                best_loss = avg_loss
            
            if epoch % 10 == 0:
                print(f"    Epoch {epoch}: loss={avg_loss:.4f}")
        
        print(f"  ✓ Best loss: {best_loss:.4f}")
        
        # Evaluate on held-out 3-hop tasks
        if held_out_3hop:
            print(f"\n  Evaluating on held-out 3-hop tasks...")
            self.evaluate_3hop(held_out_3hop)
        
        return best_loss
    
    def evaluate_3hop(self, tasks: List[Dict]):
        """Measure 3-hop accuracy before and after training."""
        baseline_acc = []
        trained_acc = []
        
        for example in tasks:
            source_idx = self.codes.index(example["source"]["code"])
            source_vec = self.embeddings[source_idx].copy()
            
            # Get 3-hop target
            target_idx = self.codes.index(example["hop_3"]["code"])
            target_vec = self.embeddings[target_idx].copy()
            
            # Baseline (no sleep)
            base_sim = self.compute_similarity(source_vec, target_vec)
            
            # Trained (with sleep)
            sleep_vec = self.sleep_pass(source_vec, target_vec, n_passes=4)
            trained_sim = self.compute_similarity(sleep_vec, target_vec)
            
            baseline_acc.append(base_sim)
            trained_acc.append(trained_sim)
        
        base_mean = np.mean(baseline_acc)
        trained_mean = np.mean(trained_acc)
        improvement = trained_mean - base_mean
        
        print(f"    Baseline 3-hop sim: {base_mean:.4f}")
        print(f"    Trained 3-hop sim:  {trained_mean:.4f}")
        print(f"    Improvement:        {improvement:+.4f} ({improvement/base_mean*100:+.1f}%)")


def main():
    parser = argparse.ArgumentParser(description="Train W_consolidate for LLM Sleep")
    parser.add_argument("--db", default="/Users/Ciphemon/.openclaw/workspace/LISA_FTM/db_523/ontology_mem_expanded.pkl")
    parser.add_argument("--test-set", default="/Users/Ciphemon/.openclaw/workspace/digi_office/data/sleep_multi_hop_test.json")
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--output", default="/Users/Ciphemon/.openclaw/workspace/digi_office/data/w_consolidate_trained.npy")
    args = parser.parse_args()
    
    print("=" * 60)
    print("PHASE 1b: Training W_consolidate")
    print("=" * 60)
    
    print("\n[1/3] Loading data...")
    trainer = Trainer(args.db, args.test_set)
    print(f"  ✓ Ontology: {len(trainer.ontology)} entries")
    print(f"  ✓ Test set: {len(trainer.test_set)} examples")
    
    print("\n[2/3] Training...")
    best_loss = trainer.train(n_epochs=args.epochs)
    
    print("\n[3/3] Saving trained weights...")
    np.save(args.output, trainer.W)
    print(f"  ✓ Saved to {args.output}")
    
    print("\n" + "=" * 60)
    print("PHASE 1b COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
