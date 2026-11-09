from dataclasses import dataclass, field


@dataclass
class Finding:
    rule: str
    severity: str
    category: str
    location: str
    evidence: str
    message: str


@dataclass
class LayerItem:
    kind: str
    text: str
    location: str
    reason: str


@dataclass
class Report:
    target: str
    verdict: str
    findings: list[Finding]
    layers: list[LayerItem]
    visible_text: str
    summary: dict[str, int] = field(default_factory=dict)
