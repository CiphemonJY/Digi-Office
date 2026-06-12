ROUTING_TABLE = {
    # ── Ontology pipeline (Ciphemon → DGX validation) ──────────────
    "expand_ontology": {
        "default": "ciphemon",
        "fallback": "hermes",
        "required_capabilities": ["python", "embeddings", "crosswalk"],
        "proxy": False,
    },
    "ontology_validate": {
        "default": "dgx_primary",
        "fallback": "dgx_secondary",
        "required_capabilities": [],
        "proxy": True,
    },
    "ontology_quality_check": {
        "default": "dgx_primary",
        "fallback": "dgx_secondary",
        "required_capabilities": [],
        "proxy": True,
    },

    # ── FHIR generation pipeline (DGX produces, DGX validates) ────
    "fhir_generate": {
        "default": "dgx_primary",
        "fallback": "dgx_secondary",
        "required_capabilities": [],
        "proxy": True,
    },
    "fhir_validate": {
        "default": "dgx_primary",
        "fallback": "dgx_secondary",
        "required_capabilities": [],
        "proxy": True,
    },
    "fhir_bundle_clean": {
        "default": "dgx_primary",
        "fallback": "dgx_secondary",
        "required_capabilities": [],
        "proxy": True,
    },

    # ── Model training / eval (DGX only) ───────────────────────────
    "llm_finetune": {
        "default": "dgx_primary",
        "fallback": "dgx_secondary",
        "required_capabilities": [],
        "proxy": True,
    },
    "model_eval": {
        "default": "dgx_primary",
        "fallback": "dgx_secondary",
        "required_capabilities": [],
        "proxy": True,
    },
    "model_export": {
        "default": "dgx_primary",
        "fallback": "dgx_secondary",
        "required_capabilities": [],
        "proxy": True,
    },

    # ── Fleet sync (Hermes dispatches to all) ─────────────────────
    "data_sync": {
        "default": "ciphemon",
        "fallback": "hermes",
        "required_capabilities": ["git"],
        "proxy": False,
    },
    "sync_fleet": {
        "default": "hermes",
        "fallback": None,
        "required_capabilities": ["ssh"],
        "proxy": False,  # Hermes loops over machines itself
    },
    "render_3d": {
        "default": "dgx_secondary",
        "fallback": "dgx_primary",
        "required_capabilities": [],
        "proxy": True,
    },
    # ── Generic remote execution (payload.commands) ─────────────────
    # Unlocks: payload.commands as shell strings, executed via SSH on DGX.
    "remote_exec": {
        "default": "dgx_primary",
        "fallback": "dgx_secondary",
        "required_capabilities": [],
        "proxy": True,
    },

    # ── LLM Sleep (Ciphemon's sleep-enhanced embedder) ─────────────
    # Paper: arXiv:2605.26099v3 — multi-pass embedding consolidation.
    "sleep_consolidation": {
        "default": "ciphemon",
        "fallback": "dgx_primary",
        "required_capabilities": ["validation"],
        "proxy": False,
    },

    # ── DGX CUDA training (dispatches to cluster) ──────────────────
    # Use for any torchrun/torch.distributed work on the 2-GPU cluster.
    # Nodes: spark (100.72.65.100) + spark-8686 (100.99.1.84).
    # Supports nnodes=1 (single-node) or nnodes=2 (distributed).
    "dgx_training": {
        "default": "dgx_primary",
        "fallback": "dgx_secondary",
        "required_capabilities": ["gpu", "cuda"],
        "proxy": True,
    },

}


def resolve_route(task_type: str) -> dict:
    return ROUTING_TABLE.get(task_type, {
        "default": None,
        "fallback": None,
        "required_capabilities": [],
        "proxy": False,
    })
