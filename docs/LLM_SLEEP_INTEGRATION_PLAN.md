# LLM Sleep Integration Plan

## Phase 1: Real Data Integration

### Overview
Integrate the LLM Sleep mechanism (inspired by the Gated Delta Net paper) into LISA_FTM to improve cross-ontology reasoning through iterative embedding consolidation during "sleep" phases.

### Data Sources
- **db_523**: 2,501 ontology entries (1,978 SNOMED-CT, 500 RXNORM, 23 LOINC)
- **384-dimensional embeddings** pre-computed for all entries
- **Crosswalk v3**: 1,898 mappings from Synthea FHIR codes to db_523

### Architecture

```
Input Query → SleepEmbedder → N Consolidation Passes → Enhanced Embedding
                    ↓
            [Iterative refinement with context gating]
                    ↓
            Similarity Search → Cross-ontology Results
```

### Components

#### 1. SleepEmbedder
- Replaces MockEmbedder with real sentence-transformers
- Loads db_523 embeddings from `ontology_mem_expanded.pkl`
- Implements N-pass consolidation with learnable weights

#### 2. Consolidation Mechanism
- **Context gating**: Attend to related concepts during sleep
- **Iterative refinement**: Each pass strengthens cross-ontology connections
- **Weight learning**: Train W_consolidate on multi-hop ground truth

#### 3. Multi-hop Test Set
- **Target**: 150 examples with k=1,2,3,4 hop labels
- **Chains**: SNOMED → LOINC → CDT → RXNORM
- **Validation**: Compare 1-pass vs 4-pass accuracy

### Implementation Steps

#### Step 1: Replace MockEmbedder
```python
from sentence_transformers import SentenceTransformer
self.model = SentenceTransformer('all-MiniLM-L6-v2')
```

#### Step 2: Create Multi-hop Test Set
- Extract concept chains from db_523 relationships
- Generate 150 labeled examples
- Validate chain correctness manually (sample)

#### Step 3: Run Validation
- Baseline: 1-pass direct embedding
- Sleep-enhanced: 4-pass with consolidation
- Metrics: Accuracy by hop depth, cosine similarity shift

#### Step 4: Commit Results
- Branch: `feat/goal-layer-live`
- Files:
  - `scripts/sleep_embedder.py`
  - `data/sleep_multi_hop_test.json`
  - `eval/sleep_phase1_results.json`

### Expected Outcomes
- 1-hop accuracy: ~85-90% (baseline ~80%)
- 3-hop accuracy: ~60-70% (baseline ~40%)
- Cosine similarity preservation: >0.99
- Norm shift: 1.5-2.5 units (indicates meaningful consolidation)

### DGX Integration
- Task type: `dgx_training`
- Target: `spark` (100.72.65.100) or `spark-8686` (100.99.1.84)
- Speedup: 1.5-1.7× over Mac Mini (2× GB10 GPUs)

### Parallel Track
- Does NOT block Phase C DP-SGD
- Independent validation pipeline
- Results inform Phase C embedding strategy

---

**Status**: Phase 1 initiated — 2026-06-11
**Owner**: Ciphemon (Mac Mini) / Hermes (WSL coordinator)
**Goal**: 857d67c6
