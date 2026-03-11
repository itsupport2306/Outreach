import json
import os
from typing import Dict, Any

# Compute project root and templates.json path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_PATH = os.path.join(PROJECT_ROOT, "templates.json")


def _load_templates() -> Dict[str, Any]:
    try:
        if os.path.exists(TEMPLATES_PATH):
            with open(TEMPLATES_PATH, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception as e:
        # Avoid hard failure if templates are malformed; return empty templates
        print(f"Error loading templates.json: {e}")
    return {"sms": {}, "email": {}}


class TemplateManager:
    _templates: Dict[str, Any]

    def __init__(self) -> None:
        self._templates = _load_templates()

    def reload(self) -> None:
        self._templates = _load_templates()

    def get_sms_template(self, name: str) -> str:
        return (
            (self._templates.get("sms") or {}).get(name) or ""
        )

    def get_email_template(self, name: str) -> Dict[str, str]:
        tpl = (self._templates.get("email") or {}).get(name) or {}
        if not isinstance(tpl, dict):
            return {"subject": "", "body": ""}
        return {
            "subject": str(tpl.get("subject") or ""),
            "body": str(tpl.get("body") or ""),
        }


# Singleton instance
template_manager = TemplateManager()
