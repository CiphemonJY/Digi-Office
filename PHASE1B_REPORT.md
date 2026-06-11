# Phase 1b Report — Scalar-α Optimization

**Date**: 2026-06-11  
**Commit**: `35c90ac` on `feat/goal-layer`

---

## Summary

Successfully trained and validated the sleep consolidation mechanism using a **single scalar parameter α** instead of the full W_consolidate matrix (295K params).

**Key Result**: Trained α improves cross-concept similarity by **+63.53%** over baseline.

---

## Method

### Architecture
```
Sleep update: S_t = S_{t-1} + α · mean(neighbor_contexts)

Where:
- neighbors = top-k similar concepts (by cosine) above threshold
- α = learned scalar (optimal: 2.339)
- norm preserved after each pass
```

### Training
- **Optimizer**: COBYLA (derivative-free, handles non-differentiable neighbor selection)
- **Data**: 288 train pairs, 96 test pairs (75/25 split)
- **Task**: Maximize cosine similarity between sleep-enhanced source and target concept
- **Held-out**: Test set never seen during training

### Hyperparameters
- `top_k = 5` neighbors
- `threshold = 0.6` (cosine gate)
- `n_passes = 2` (training), `n_passes = 4` (evaluation)

---

## Results

| Metric | Value |
|--------|-------|
| Baseline cosine | 0.4698 |
| Trained (2-pass) | 0.7708 |
| Trained (4-pass) | 0.7683 |
| **Improvement** | **+0.2985 (+63.53%)** |
| Optimal α | 2.339 |
| Training success | Yes |

**Interpretation**:
- The paper's core claim is **validated**: learned sleep improves cross-concept similarity
- 2-pass and 4-pass converge to similar values — diminishing returns after 2 passes
- High α (2.339) means strong context blending is optimal for this ontology

---

## What Didn't Work

### Full W_consolidate (v1/v2)
- 295K parameters with finite-difference gradient → -24.4% degradation
- Gradient through argmax neighbor selection is non-differentiable
- **Lesson**: Start with scalar, then scale up with PyTorch autograd

### Scalar-only tanh (v3)
- α=0.266 but 0% improvement on test
- tanh(current) without neighbor context is just scaling
- **Lesson**: Context matters — the paper's mechanism is about neighbor blending

---

## Files

| File | Purpose |
|------|---------|
| `train_alpha_context.py` | Main training script (v4, working) |
| `data/alpha_context.json` | Results |
| `prototype_sleep_embedder.py` | Original embedder with W_consolidate |

---

## Next: Phase 2

**Coordinator Integration**:
1. Register `sleep_consolidation` task type with coordinator
2. Agent receives task → runs sleep → returns enhanced embedding
3. Parallel track with Phase C DP-SGD

**Ready to proceed**, Tamer.
