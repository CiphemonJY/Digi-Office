ROUTING_TABLE = {
    "expand_ontology": {
        "default": "ciphemon",
        "fallback": "hermes",
        "required_capabilities": ["python", "embeddings"],
        "proxy": False,
    },
    "fhir_validate": {
        "default": "jetson",
        "fallback": "hermes",
        "required_capabilities": [],
        "proxy": True,
    },
    "llm_finetune": {
        "default": "dgx_primary",
        "fallback": "dgx_secondary",
        "required_capabilities": [],
        "proxy": True,
    },
    "render_3d": {
        "default": "dgx_secondary",
        "fallback": "dgx_primary",
        "required_capabilities": [],
        "proxy": True,
    },
    "data_sync": {
        "default": "hermes",
        "fallback": None,
        "required_capabilities": ["git", "ssh"],
        "proxy": False,
    },
}


def resolve_route(task_type: str) -> dict:
    return ROUTING_TABLE.get(task_type, {
        "default": None,
        "fallback": None,
        "required_capabilities": [],
        "proxy": False,
    })
