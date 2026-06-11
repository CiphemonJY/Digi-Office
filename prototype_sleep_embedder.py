#!/usr/bin/env python3
"""
Prototype Sleep Embedder for LLM Sleep Phase 1
Replaces MockEmbedder with real sentence-transformers + sleep consolidation.

Usage:
    python prototype_sleep_embedder.py --db /path/to/ontology_mem_expanded.pkl --test --create-test-set
"""

import argparse
import json
import pickle
import random
import numpy as np
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import defaultdict, Counter

# Try to import sentence-transformers, fallback to mock for testing
try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SENTENCE_TRANSFORMERS_AVAILABLE = False
    print("Warning: sentence-transformers not available. Using mock embeddings.")


class SleepEmbedder:
    """Sleep-enhanced embedder with N-pass consolidation."""
    
    def __init__(self, ontology_path: str, model_name: str = 'all-MiniLM-L6-v2'):
        self.ontology_path = Path(ontology_path)
        self.model_name = model_name
        
        # Load ontology
        self.ontology = self._load_ontology()
        self.code_to_entry = {entry[0]: entry for entry in self.ontology}
        
        # Build embedding matrix — handle variable dimensions (truncate to 384)
        self.codes = [entry[0] for entry in self.ontology]
        self.systems = [entry[1] for entry in self.ontology]
        self.displays = [entry[2] for entry in self.ontology]
        
        # Convert embeddings, ensuring consistent 384-dim
        raw_embeddings = []
        for entry in self.ontology:
            vec = np.array(entry[3], dtype=np.float32)
            if len(vec) > 384:
                vec = vec[:384]  # Truncate longer embeddings
            elif len(vec) < 384:
                vec = np.pad(vec, (0, 384 - len(vec)), mode='constant')  # Pad shorter
            raw_embeddings.append(vec)
        
        self.embeddings = np.array(raw_embeddings)
        
        # Load or create sentence transformer
        if SENTENCE_TRANSFORMERS_AVAILABLE:
            self.model = SentenceTransformer(model_name)
            self.embedding_dim = self.model.get_sentence_embedding_dimension()
        else:
            self.model = None
            self.embedding_dim = 384
        
        # Learnable consolidation weights (initialized to identity)
        self.W_consolidate = np.eye(self.embedding_dim) * 0.1
        self.context_gate_threshold = 0.6
        
    def _load_ontology(self) -> List[Tuple]:
        """Load ontology from pickle file."""
        with open(self.ontology_path, 'rb') as f:
            return pickle.load(f)
    
    def embed(self, text: str) -> np.ndarray:
        """Embed text using sentence-transformers or mock fallback."""
        if self.model:
            return self.model.encode(text)
        else:
            # Mock: deterministic hash-based embedding
            hash_val = hash(text) % (2**32)
            np.random.seed(hash_val)
            return np.random.randn(self.embedding_dim).astype(np.float32) * 0.1
    
    def find_similar(self, query_vec: np.ndarray, top_k: int = 5, 
                     system_filter: Optional[str] = None) -> List[Tuple]:
        """Find similar ontology entries by cosine similarity."""
        # Normalize query
        query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-8)
        
        # Compute similarities
        emb_norm = self.embeddings / (np.linalg.norm(self.embeddings, axis=1, keepdims=True) + 1e-8)
        similarities = np.dot(emb_norm, query_norm)
        
        # Filter by system if specified
        indices = np.arange(len(self.codes))
        if system_filter:
            mask = [s == system_filter for s in self.systems]
            indices = indices[mask]
            similarities = similarities[mask]
        
        # Get top-k
        top_indices = np.argsort(similarities)[-top_k:][::-1]
        results = []
        for idx in top_indices:
            real_idx = indices[idx] if system_filter else idx
            results.append((
                self.codes[real_idx],
                self.systems[real_idx],
                self.displays[real_idx],
                float(similarities[idx])
            ))
        return results
    
    def sleep_consolidate(self, query_vec: np.ndarray, context: List[str],
                          n_passes: int = 4) -> np.ndarray:
        """
        Perform N-pass sleep consolidation.
        
        Each pass:
        1. Find similar concepts (sleep context)
        2. Apply learned consolidation weights
        3. Gate by similarity threshold
        """
        current = query_vec.copy()
        
        for pass_idx in range(n_passes):
            # Find sleep context (similar concepts) using index
            similar = self.find_similar(current, top_k=10)
            
            # Get context vectors from the normalized embedding matrix
            context_vecs = []
            for code, system, display, sim in similar:
                if sim > self.context_gate_threshold:
                    idx = self.codes.index(code)
                    context_vecs.append(self.embeddings[idx].copy())
            
            if not context_vecs:
                continue
            
            # Average context (now guaranteed 384-dim)
            context_mean = np.mean(context_vecs, axis=0)
            
            # Apply consolidation: v_new = v + W * context
            delta = np.dot(self.W_consolidate, context_mean)
            current = current + delta * (1.0 / (pass_idx + 1))  # Decaying influence
            
            # Normalize to prevent drift
            current = current / (np.linalg.norm(current) + 1e-8) * np.linalg.norm(query_vec)
        
        return current
    
    def compute_similarity(self, vec1: np.ndarray, vec2: np.ndarray) -> float:
        """Compute cosine similarity between two vectors."""
        return float(np.dot(vec1, vec2) / (np.linalg.norm(vec1) * np.linalg.norm(vec2) + 1e-8))


def create_multi_hop_test_set(embedder: SleepEmbedder, n_examples: int = 150) -> List[Dict]:
    """
    Create multi-hop test set from ontology relationships.
    
    Strategy:
    1. Start with SNOMED conditions
    2. Find related concepts across ontologies (based on semantic similarity)
    3. Label with hop count (1-4)
    """
    test_set = []
    
    # Get all SNOMED entries
    snomed_entries = [e for e in embedder.ontology if e[1] == 'SNOMED-CT']
    
    # Create chains by finding semantic hops
    for i in range(min(n_examples, len(snomed_entries))):
        source = random.choice(snomed_entries)
        # Get source vector from normalized embeddings
        source_idx = embedder.codes.index(source[0])
        source_vec = embedder.embeddings[source_idx].copy()
        
        # 1-hop: closest in same ontology
        similar_same = embedder.find_similar(source_vec, top_k=2, system_filter='SNOMED-CT')
        if len(similar_same) < 2:
            continue
        hop1 = similar_same[1]  # Skip self
        
        # 2-hop: jump to different ontology
        hop1_idx = embedder.codes.index(hop1[0])
        hop1_vec = embedder.embeddings[hop1_idx].copy()
        similar_cross = embedder.find_similar(hop1_vec, top_k=5)
        cross_system = [s for s in similar_cross if s[1] != 'SNOMED-CT']
        if not cross_system:
            continue
        hop2 = cross_system[0]
        
        # 3-hop: continue chain
        hop2_idx = embedder.codes.index(hop2[0])
        hop2_vec = embedder.embeddings[hop2_idx].copy()
        similar_3 = embedder.find_similar(hop2_vec, top_k=5)
        different_system = [s for s in similar_3 if s[1] != hop2[1]]
        if not different_system:
            continue
        hop3 = different_system[0]
        
        # 4-hop: final jump
        hop3_idx = embedder.codes.index(hop3[0])
        hop3_vec = embedder.embeddings[hop3_idx].copy()
        similar_4 = embedder.find_similar(hop3_vec, top_k=5)
        final_system = [s for s in similar_4 if s[1] not in [source[1], hop3[1]]]
        if not final_system:
            continue
        hop4 = final_system[0]
        
        test_set.append({
            "id": f"mh_{i:04d}",
            "source": {"code": source[0], "system": source[1], "display": source[2]},
            "hop_1": {"code": hop1[0], "system": hop1[1], "display": hop1[2], "similarity": hop1[3]},
            "hop_2": {"code": hop2[0], "system": hop2[1], "display": hop2[2], "similarity": hop2[3]},
            "hop_3": {"code": hop3[0], "system": hop3[1], "display": hop3[2], "similarity": hop3[3]},
            "hop_4": {"code": hop4[0], "system": hop4[1], "display": hop4[2], "similarity": hop4[3]},
        })
    
    return test_set


def validate_sleep_effect(embedder: SleepEmbedder, test_set: List[Dict], 
                          n_passes_list: List[int] = [1, 2, 4, 6]) -> Dict:
    """Validate sleep consolidation effect on multi-hop accuracy."""
    
    results = {n: {"accuracies": [], "cosine_shifts": [], "norm_shifts": []} 
               for n in n_passes_list}
    
    for example in test_set[:50]:  # Validate on subset
        source_idx = embedder.codes.index(example["source"]["code"])
        source_vec = embedder.embeddings[source_idx].copy()
        
        for n_passes in n_passes_list:
            # Baseline (no sleep)
            baseline_vec = source_vec.copy()
            
            # Sleep-enhanced
            sleep_vec = embedder.sleep_consolidate(
                source_vec, 
                context=[example["source"]["display"]],
                n_passes=n_passes
            )
            
            # Measure shifts
            cosine_shift = embedder.compute_similarity(source_vec, sleep_vec)
            norm_shift = float(np.linalg.norm(sleep_vec) - np.linalg.norm(source_vec))
            
            # Check accuracy at each hop depth
            hop_accuracies = []
            for hop_depth in [1, 2, 3]:
                hop_key = f"hop_{hop_depth}"
                if hop_key in example:
                    hop_idx = embedder.codes.index(example[hop_key]["code"])
                    hop_vec = embedder.embeddings[hop_idx].copy()
                    
                    # Baseline similarity
                    base_sim = embedder.compute_similarity(baseline_vec, hop_vec)
                    
                    # Sleep similarity
                    sleep_sim = embedder.compute_similarity(sleep_vec, hop_vec)
                    
                    # Accuracy: did sleep improve similarity?
                    hop_accuracies.append(sleep_sim > base_sim)
            
            results[n_passes]["accuracies"].append(np.mean(hop_accuracies) if hop_accuracies else 0)
            results[n_passes]["cosine_shifts"].append(cosine_shift)
            results[n_passes]["norm_shifts"].append(norm_shift)
    
    # Aggregate
    summary = {}
    for n_passes in n_passes_list:
        summary[n_passes] = {
            "accuracy": float(np.mean(results[n_passes]["accuracies"])),
            "cosine_sim": float(np.mean(results[n_passes]["cosine_shifts"])),
            "norm_shift": float(np.mean(results[n_passes]["norm_shifts"])),
            "n_tested": len(results[n_passes]["accuracies"])
        }
    
    return summary


def main():
    parser = argparse.ArgumentParser(description="LLM Sleep Phase 1 Prototype")
    parser.add_argument("--db", default="/Users/Ciphemon/.openclaw/workspace/LISA_FTM/db_523/ontology_mem_expanded.pkl",
                        help="Path to ontology pickle file")
    parser.add_argument("--create-test-set", action="store_true", help="Create multi-hop test set")
    parser.add_argument("--validate", action="store_true", help="Run validation")
    parser.add_argument("--test", action="store_true", help="Quick test run")
    parser.add_argument("--output", default="/Users/Ciphemon/.openclaw/workspace/digi_office/data",
                        help="Output directory")
    args = parser.parse_args()
    
    print("=" * 60)
    print("LLM SLEEP PHASE 1 PROTOTYPE")
    print("=" * 60)
    
    # Initialize embedder
    print("\n[1/4] Loading ontology...")
    embedder = SleepEmbedder(args.db)
    print(f"  ✓ Loaded {len(embedder.ontology)} entries")
    print(f"  ✓ Embedding dim: {embedder.embedding_dim}")
    print(f"  ✓ Model: {embedder.model_name if embedder.model else 'Mock (sentence-transformers not installed)'}")
    
    # Quick test
    if args.test:
        print("\n[2/4] Running quick test...")
        test_vec = embedder.embed("Type 2 diabetes mellitus")
        similar = embedder.find_similar(test_vec, top_k=3)
        print(f"  Query: 'Type 2 diabetes mellitus'")
        for code, system, display, sim in similar:
            print(f"    → {code} ({system}): {display} [sim={sim:.4f}]")
        
        # Test sleep consolidation
        print("\n  Testing sleep consolidation (4 passes)...")
        sleep_vec = embedder.sleep_consolidate(test_vec, context=["diabetes mellitus"], n_passes=4)
        cosine_shift = embedder.compute_similarity(test_vec, sleep_vec)
        norm_shift = np.linalg.norm(sleep_vec) - np.linalg.norm(test_vec)
        print(f"    Cosine sim (baseline vs sleep): {cosine_shift:.4f}")
        print(f"    Norm shift: {norm_shift:.4f}")
    
    # Create test set
    if args.create_test_set:
        print("\n[3/4] Creating multi-hop test set...")
        test_set = create_multi_hop_test_set(embedder, n_examples=150)
        
        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        test_path = output_dir / "sleep_multi_hop_test.json"
        with open(test_path, 'w') as f:
            json.dump(test_set, f, indent=2)
        
        print(f"  ✓ Created {len(test_set)} examples")
        print(f"  ✓ Saved to {test_path}")
        
        # Print samples
        print("\n  Sample chains:")
        for ex in test_set[:3]:
            print(f"    {ex['id']}: {ex['source']['display']}")
            for hop in [1, 2, 3, 4]:
                hop_key = f"hop_{hop}"
                if hop_key in ex:
                    h = ex[hop_key]
                    print(f"      → [{hop}-hop] {h['display']} ({h['system']}, sim={h['similarity']:.3f})")
    
    # Validation
    if args.validate:
        print("\n[4/4] Running validation...")
        
        # Load or create test set
        test_path = Path(args.output) / "sleep_multi_hop_test.json"
        if test_path.exists():
            with open(test_path) as f:
                test_set = json.load(f)
        else:
            print("  Test set not found. Creating...")
            test_set = create_multi_hop_test_set(embedder, n_examples=150)
        
        results = validate_sleep_effect(embedder, test_set)
        
        print("\n  Results:")
        print("  " + "-" * 50)
        print(f"  {'Passes':<10} {'Accuracy':<12} {'Cosine Sim':<14} {'Norm Shift':<12}")
        print("  " + "-" * 50)
        for n_passes, metrics in results.items():
            print(f"  {n_passes:<10} {metrics['accuracy']:.4f}      {metrics['cosine_sim']:.4f}        {metrics['norm_shift']:.4f}")
        print("  " + "-" * 50)
        
        # Save results
        results_path = Path(args.output) / "sleep_phase1_results.json"
        with open(results_path, 'w') as f:
            json.dump(results, f, indent=2)
        print(f"\n  ✓ Results saved to {results_path}")
    
    print("\n" + "=" * 60)
    print("PHASE 1 PROTOTYPE COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
