"""Attack-surface asset extraction from the blackboard.

Builds a typed asset graph (domains, IPs, endpoints, services, technology,
cloud/SaaS, identity providers, CVEs) from fact prose and findings, with
relations between them. Every node and edge carries the fact/finding ids that
established it, so the map is an auditable rendering of the blackboard — never
a parallel data path. Works on running projects as well as finished ones.

Render-time and deterministic by design (same philosophy as findings
extraction); a durable ``assets``/``relations`` table is the natural v2 once
extraction quality is proven.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict

URL_PATTERN = re.compile(r"https?://[a-zA-Z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
IP_PATTERN = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
HOST_PATTERN = re.compile(r"\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,12}\b")
CVE_PATTERN = re.compile(r"\bCVE-\d{4}-\d{4,8}\b", re.IGNORECASE)
PORT_PATTERN = re.compile(r"(?<![\d.])(?:port\s+|:)(\d{2,5})(?![\d.])", re.IGNORECASE)

PROBE_MARKERS = ("evil", "fuzz", "randomzz", "attacker", "probe-", "-probe")
PROBE_SUFFIXES = ("example.com", "example.org", "example.net", "testattacker")

FILE_SUFFIXES = (
    ".txt", ".json", ".html", ".htm", ".js", ".css", ".md", ".sh", ".py", ".yaml",
    ".yml", ".xml", ".bin", ".png", ".jpg", ".svg", ".ico", ".axd", ".aspx", ".svc",
    ".php", ".do", ".properties", ".body", ".done", ".map", ".tsv", ".log", ".config",
    ".data", ".ts", ".go",
)

RESOLVER_IPS = {"8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1", "9.9.9.9"}

# Hostname pattern -> (provider, service label). First match wins.
CLOUD_HOST_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"(^|\.)execute-api\.[a-z0-9-]+\.amazonaws\.com$", "AWS", "AWS API Gateway (Lambda)"),
    (r"\.elb\.amazonaws\.com$", "AWS", "AWS ELB"),
    (r"s3[.-][a-z0-9-]*\.?s3[.-]?[a-z0-9-]*\.amazonaws\.com$|\.s3\.amazonaws\.com$|\.s3-[a-z0-9-]+\.amazonaws\.com$", "AWS", "AWS S3"),
    (r"\.cloudfront\.net$", "AWS", "AWS CloudFront"),
    (r"\.amazonaws\.com$", "AWS", "AWS"),
    (r"\.azure-api\.net$", "Azure", "Azure API Management"),
    (r"\.azurewebsites\.net$", "Azure", "Azure App Service"),
    (r"\.azurefd\.net$", "Azure", "Azure Front Door"),
    (r"\.cloudapp\.azure\.com$|\.cloudapp\.net$", "Azure", "Azure Cloud Service"),
    (r"api\.applicationinsights\.io$|\.services\.visualstudio\.com$|\.applicationinsights\.azure\.com$", "Azure", "Azure Monitor"),
    (r"login\.microsoftonline\.com$|login\.microsoft\.com$|login\.windows\.net$|graph\.microsoft\.com$", "Microsoft", "Microsoft Entra ID"),
    (r"\.force\.com$|\.lightning\.force\.com$", "Salesforce", "Salesforce"),
    (r"\.coupahost\.com$", "Coupa", "Coupa"),
    (r"\.service-now\.com$", "ServiceNow", "ServiceNow"),
    (r"\.m-files\.com$", "M-Files", "M-Files cloud vault"),
    (r"\.datadoghq\.eu$|\.datadoghq\.com$", "Datadog", "Datadog"),
    (r"\.pardot\.com$", "Salesforce", "Salesforce Pardot"),
    (r"\.eficode\.io$", "Eficode", "Eficode (Artifactory/Jira)"),
    (r"sitefinity\.fi$|\.sitefinity\.com$|\.sitefinity\.digital$", "Progress", "Sitefinity Cloud"),
    (r"\.railway\.app$", "Railway", "Railway"),
    (r"\.siteground\.biz$|\.sgts\.siteground\.net$", "SiteGround", "SiteGround hosting"),
    (r"\.cloudvault\.m-files\.com$", "M-Files", "M-Files cloud vault"),
)

TECH_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bnext\.?js\b", "Next.js"),
    (r"\bwordpress\b", "WordPress"),
    (r"\bnginx\b", "nginx"),
    (r"\bapache httpd\b|\bapache\b", "Apache"),
    (r"\biis\b|asp\.net", "Microsoft IIS / ASP.NET"),
    (r"\bsitefinity\b", "Progress Sitefinity"),
    (r"\btelerik\b", "Telerik UI"),
    (r"\bkeycloak\b|auth/realms/", "Keycloak"),
    (r"\bwcf\b", "WCF services"),
    (r"\breact\b", "React"),
    (r"\bnode(\.js)?\b", "Node.js"),
    (r"\bphp\b", "PHP"),
    (r"\bfortigate\b|ssl-?vpn", "FortiGate SSL-VPN"),
    (r"\bsap fiori\b|fiori\b", "SAP Fiori"),
    (r"\bpowerbi\b|power bi\b", "Microsoft Power BI"),
    (r"\bsharepoint\b", "Microsoft SharePoint"),
    (r"\bexchange\b", "Microsoft Exchange"),
    (r"\bazure ad\b|\bentra\b", "Microsoft Entra ID"),
    (r"\bzoho\b", "Zoho"),
    (r"\bmsal\b", "MSAL"),
)

CLOUD_KEYWORDS: tuple[tuple[str, str, str], ...] = (
    (r"\bcloudflare\b", "Cloudflare", "Cloudflare WAF/CDN"),
    (r"\brailway\b", "Railway", "Railway"),
    (r"\baws\b|amazon web services", "AWS", "AWS"),
    (r"\bazure\b", "Azure", "Azure"),
)

# Providers where the company hosts its own workloads vs external SaaS holding
# company data — the distinction that matters when reviewing exposure.
SAAS_PROVIDERS = frozenset(
    {"Salesforce", "Coupa", "ServiceNow", "M-Files", "Datadog", "Eficode", "Pardot"}
)
HOSTING_PROVIDERS = frozenset(
    {"AWS", "Azure", "Cloudflare", "Railway", "SiteGround", "Progress", "Vercel", "Microsoft"}
)

RESOLVES_RE = re.compile(r"resolves?\s+(?:to|against)|points?\s+to|A\s+record|CNAME|canonical name", re.IGNORECASE)
SERVED_RE = re.compile(
    r"served\s+by|hosted\s+(?:on|by)|running\s+on|deployed\s+on|edge\s+prox|proxied|behind|origin|direct\s+connect|bypass",
    re.IGNORECASE,
)
RUNS_RE = re.compile(r"\bruns\b|powered\s+by|built\s+with|using|version\s+of|\bis\s+on\b", re.IGNORECASE)

SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
VERSION_RE = re.compile(r"v?(\d+(?:\.\d+)+)")

MAX_FACT_SNIPPET = 4000


def build_asset_map(conn: sqlite3.Connection, project_id: str) -> dict:
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        raise LookupError(f"project not found: {project_id}")

    facts = conn.execute(
        "SELECT id, description FROM facts WHERE project_id = ? AND id NOT IN ('origin', 'goal')",
        (project_id,),
    ).fetchall()
    findings = conn.execute(
        "SELECT id, fact_id, title, severity, affected_target, description FROM findings WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    intents = conn.execute(
        "SELECT id, description, to_fact_id, worker, created_at, concluded_at FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    intent_by_fact = {row["to_fact_id"]: row["id"] for row in intents if row["to_fact_id"]}
    evidence_by_fact: dict[str, list[dict]] = defaultdict(list)
    for row in conn.execute(
        "SELECT id, fact_id, kind, path FROM evidence WHERE project_id = ? AND fact_id IS NOT NULL",
        (project_id,),
    ):
        evidence_by_fact[row["fact_id"]].append({"id": row["id"], "kind": row["kind"], "path": row["path"]})

    builder = _GraphBuilder()

    fact_texts: dict[str, str] = {}
    for fact in facts:
        fact_texts[fact["id"]] = fact["description"]
        context: dict[str, str | None] = {"domain": None, "ip": None}
        for sentence in _sentences(fact["description"]):
            builder.ingest_sentence(sentence, fact_id=fact["id"], context=context)

    for finding in findings:
        builder.anchor_finding(
            finding_id=finding["id"],
            severity=finding["severity"],
            target_text=f"{finding['affected_target'] or ''} {finding['title']} {finding['description']}",
        )

    builder.link_domain_hierarchy()
    _classify_categories(builder.nodes)
    _attach_intents(builder.nodes, intents, intent_by_fact)

    nodes = sorted(builder.nodes.values(), key=lambda n: (n["type"], n["label"].lower()))
    edges = sorted(builder.edges.values(), key=lambda e: (e["type"], e["source"], e["target"]))

    counts: dict[str, int] = defaultdict(int)
    providers: dict[str, set[str]] = defaultdict(set)
    category_counts: dict[str, int] = defaultdict(int)
    groups: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for node in nodes:
        counts[node["type"]] += 1
        category_counts[node["category"]] += 1
        if node.get("group"):
            groups[node["category"]][node["group"]] += 1
        if node.get("provider"):
            providers[node["provider"]].add(node["label"])

    return {
        "project": {
            "id": project_id,
            "title": project["title"],
            "status": project["status"],
        },
        "summary": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "counts": dict(sorted(counts.items())),
            "categories": dict(sorted(category_counts.items())),
            "groups": {category: dict(sorted(group.items())) for category, group in sorted(groups.items())},
            "providers": {provider: sorted(labels) for provider, labels in sorted(providers.items())},
        },
        "nodes": nodes,
        "edges": edges,
        "facts": {
            fact_id: {
                "text": text[:MAX_FACT_SNIPPET],
                "evidence": evidence_by_fact.get(fact_id, []),
                "intent_id": intent_by_fact.get(fact_id),
            }
            for fact_id, text in fact_texts.items()
        },
        "intents": {
            row["id"]: {
                "description": row["description"][:800],
                "worker": row["worker"],
                "open": row["to_fact_id"] is None,
            }
            for row in intents
        },
    }


def _attach_intents(nodes: dict[str, dict], intents: list, intent_by_fact: dict[str, str]) -> None:
    """Record which intents touch each asset.

    Two directions: the intent whose description mentions the asset (declared
    exploration targeting it), and the intents that produced the asset's
    establishing facts.
    """
    for node in nodes.values():
        touched: set[str] = set()
        for fact_id in node["fact_ids"]:
            producing = intent_by_fact.get(fact_id)
            if producing:
                touched.add(producing)
        label = node["label"]
        if label:
            pattern = re.compile(rf"(?<![\w.-]){re.escape(label)}(?![\w-])", re.IGNORECASE)
            for intent in intents:
                if pattern.search(intent["description"]):
                    touched.add(intent["id"])
        node["intent_ids"] = sorted(touched)


def _classify_categories(nodes: dict[str, dict]) -> None:
    """Assign each node a list category and, where meaningful, a group key.

    Categories drive the inventory views: cloud vs third-party SaaS, domains
    grouped under their registrable parent, endpoints grouped under their host.
    """
    for node in nodes.values():
        node_type = node["type"]
        if node_type == "cloud":
            provider = node.get("provider") or ""
            node["category"] = "saas" if provider in SAAS_PROVIDERS else "cloud"
            node["group"] = provider or "Other"
        elif node_type == "idp":
            node["category"] = "idp"
            node["group"] = node.get("provider") or "Identity"
        elif node_type == "domain":
            node["category"] = "domain"
            # Platform-managed hostnames (*.elb.amazonaws.com, *.azure-api.net, …)
            # group under their platform, not the meaningless registrable parent.
            node["group"] = node.get("cloud") or node.get("parent") or node["label"]
        elif node_type == "endpoint":
            node["category"] = "endpoint"
            node["group"] = node.get("parent") or ""
        elif node_type == "probe":
            node["category"] = "probe"
            node["group"] = None
        else:
            node["category"] = node_type
            node["group"] = None


def _sentences(text: str) -> list[str]:
    parts: list[str] = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*• ").strip()
        if not line:
            continue
        for sentence in re.split(r"(?<=[.!?;])\s+", line):
            sentence = sentence.strip()
            if len(sentence) >= 8:
                parts.append(sentence)
    return parts


def _is_valid_ip(token: str) -> bool:
    if token in RESOLVER_IPS:
        return False
    parts = token.split(".")
    if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return False
    first = int(parts[0])
    if first in (0, 10, 127):  # this-node, private, loopback
        return False
    if first == 169 and parts[1] == "254":  # link-local incl. cloud metadata
        return False
    if first == 172 and 16 <= int(parts[1]) <= 31:
        return False
    if first == 192 and parts[1] == "168":
        return False
    return True


def _classify_host(host: str) -> str | None:
    """Return node type for a hostname token, or None for noise."""
    lowered = host.lower()
    if any(lowered.endswith(suffix) for suffix in PROBE_SUFFIXES) or any(marker in lowered for marker in PROBE_MARKERS):
        return "probe"
    if any(lowered.endswith(suffix) for suffix in FILE_SUFFIXES):
        return None
    labels = lowered.split(".")
    if any(not re.fullmatch(r"[a-z0-9-]+", label) for label in labels):
        return None
    if any(label.startswith("-") or label.endswith("-") for label in labels):
        return None
    # Prose artifacts like Class.create.constructor / Telerik.Web.UI carry case.
    if any(ch.isupper() for ch in host):
        return None
    return "domain"


def _cloud_for_host(host: str) -> tuple[str, str] | None:
    lowered = host.lower()
    for pattern, provider, label in CLOUD_HOST_PATTERNS:
        if re.search(pattern, lowered):
            return provider, label
    return None


def _parent_domain(host: str) -> str | None:
    labels = host.split(".")
    if len(labels) < 3:
        return None
    return ".".join(labels[-2:])


class _GraphBuilder:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: dict[str, dict] = {}

    # -- nodes -------------------------------------------------------------

    def node(self, node_id: str, node_type: str, label: str, *, fact_id: str | None = None, **attrs) -> dict:
        node = self.nodes.get(node_id)
        if node is None:
            node = {
                "id": node_id,
                "type": node_type,
                "label": label,
                "severity": None,
                "negative_count": 0,
                "fact_ids": [],
                "finding_ids": [],
                **attrs,
            }
            self.nodes[node_id] = node
        for key, value in attrs.items():
            if value is not None and (key not in node or node[key] is None):
                node[key] = value
        if fact_id and fact_id not in node["fact_ids"]:
            node["fact_ids"].append(fact_id)
        return node

    def edge(self, source: str, target: str, edge_type: str, *, fact_id: str | None = None, confidence: str = "weak") -> None:
        if source == target:
            return
        key = f"{source}-{edge_type}->{target}"
        edge = self.edges.get(key)
        if edge is None:
            edge = {"source": source, "target": target, "type": edge_type, "confidence": confidence, "fact_ids": []}
            self.edges[key] = edge
        if fact_id and fact_id not in edge["fact_ids"]:
            edge["fact_ids"].append(fact_id)
        if confidence == "strong":
            edge["confidence"] = "strong"

    # -- ingestion -----------------------------------------------------------

    def ingest_sentence(self, sentence: str, *, fact_id: str, context: dict[str, str | None] | None = None) -> None:
        context = context if context is not None else {}
        urls = [u for u in (self._clean_url(u) for u in URL_PATTERN.findall(sentence)) if u]
        no_urls = URL_PATTERN.sub(" ", sentence)
        hosts = [h for h in HOST_PATTERN.findall(no_urls) if _classify_host(h)]
        ips = [ip for ip in IP_PATTERN.findall(no_urls) if _is_valid_ip(ip)]
        cves = [c.upper() for c in CVE_PATTERN.findall(sentence)]

        domain_ids: list[str] = []
        for host in dict.fromkeys(hosts):
            host_type = _classify_host(host)
            if host_type is None:
                continue
            provider, cloud_label = _cloud_for_host(host) or (None, None)
            node_type = "idp" if (provider == "Microsoft" and cloud_label and "Entra" in cloud_label) else host_type
            node = self.node(
                f"{node_type}:{host}",
                node_type,
                host,
                fact_id=fact_id,
                provider=provider,
                cloud=cloud_label,
                parent=_parent_domain(host) if host_type == "domain" else None,
            )
            if host_type in ("domain", "idp"):
                domain_ids.append(node["id"])
            if cloud_label:
                cloud_node = self.node(f"cloud:{cloud_label}", "cloud", cloud_label, fact_id=fact_id, provider=provider)
                self.edge(node["id"], cloud_node["id"], "hosted_on", fact_id=fact_id, confidence="strong")

        ip_ids: list[str] = []
        for ip in dict.fromkeys(ips):
            node = self.node(f"ip:{ip}", "ip", ip, fact_id=fact_id)
            ip_ids.append(node["id"])

        for url in urls:
            host = url.split("//", 1)[1].split("/", 1)[0].split(":")[0].lower()
            path = "/" + url.split("//", 1)[1].split("/", 1)[1] if "/" in url.split("//", 1)[1] else ""
            host_node_type = _classify_host(host)
            if host_node_type in ("domain", "probe"):
                provider, cloud_label = _cloud_for_host(host) or (None, None)
                self.node(
                    f"{host_node_type}:{host}", host_node_type, host,
                    fact_id=fact_id, provider=provider, cloud=cloud_label,
                    parent=_parent_domain(host) if host_node_type == "domain" else None,
                )
                if host_node_type == "domain":
                    host_id = f"domain:{host}"
                    if host_id not in domain_ids:
                        domain_ids.append(host_id)
            if path and path != "/":
                endpoint_class = "api" if re.search(r"/api|/auth|/oauth|/v\d|signin|login|token", path, re.IGNORECASE) else "web"
                self.node(
                    f"endpoint:{url}",
                    "endpoint",
                    url,
                    fact_id=fact_id,
                    endpoint_class=endpoint_class,
                    parent=host,
                )
                if _classify_host(host) == "domain":
                    self.edge(f"endpoint:{url}", f"domain:{host}", "belongs_to", fact_id=fact_id, confidence="strong")

        tech_ids: list[str] = []
        cloud_ids: list[str] = []
        lowered = sentence.lower()
        for pattern, label in TECH_PATTERNS:
            match = re.search(pattern, lowered)
            if not match:
                continue
            tail = sentence[match.start():match.start() + 60]
            version_match = VERSION_RE.search(tail)
            tech_node = self.node(
                f"tech:{label}", "tech", label, fact_id=fact_id,
                version=version_match.group(1) if version_match else None,
            )
            tech_ids.append(tech_node["id"])
        for pattern, provider, label in CLOUD_KEYWORDS:
            if re.search(pattern, lowered):
                cloud_node = self.node(f"cloud:{label}", "cloud", label, fact_id=fact_id, provider=provider)
                if cloud_node["id"] not in cloud_ids:
                    cloud_ids.append(cloud_node["id"])

        cve_ids = []
        for cve in dict.fromkeys(cves):
            node = self.node(f"cve:{cve}", "cve", cve, fact_id=fact_id)
            cve_ids.append(node["id"])

        # Anaphora: prose constantly says "the host runs…" / "the device is…";
        # when a sentence has no subject asset, attribute it to the fact's last
        # mentioned domain so relations survive elided subjects.
        if domain_ids:
            context["domain"] = domain_ids[-1]
        if ip_ids:
            context["ip"] = ip_ids[-1]
        implicit_domain = context.get("domain") if not domain_ids else None
        subjects = domain_ids or ([implicit_domain] if implicit_domain else [])

        # services: host/ip + port in the same sentence
        for port_raw in dict.fromkeys(PORT_PATTERN.findall(sentence)):
            port = int(port_raw)
            if not (1 <= port <= 65535) or port in (80, 443):
                continue
            anchor = (domain_ids + ip_ids + subjects)[:1]
            if not anchor:
                continue
            anchor_label = self.nodes[anchor[0]]["label"]
            service_id = f"service:{anchor_label}:{port}"
            self.node(service_id, "service", f"{anchor_label}:{port}", fact_id=fact_id, port=port)
            self.edge(anchor[0], service_id, "exposes", fact_id=fact_id, confidence="strong")

        self._infer_relations(sentence, fact_id, subjects, ip_ids, tech_ids, cve_ids, cloud_ids)

    def _infer_relations(
        self,
        sentence: str,
        fact_id: str,
        domain_ids: list[str],
        ip_ids: list[str],
        tech_ids: list[str],
        cve_ids: list[str],
        cloud_ids: list[str],
    ) -> None:
        resolves = bool(RESOLVES_RE.search(sentence))
        served = bool(SERVED_RE.search(sentence))
        runs = bool(RUNS_RE.search(sentence))

        for domain in domain_ids:
            for ip in ip_ids:
                if resolves:
                    self.edge(domain, ip, "resolves_to", fact_id=fact_id, confidence="strong")
                elif served:
                    self.edge(domain, ip, "served_by", fact_id=fact_id, confidence="strong")
                else:
                    self.edge(domain, ip, "associated", fact_id=fact_id, confidence="weak")
            for cloud in cloud_ids:
                self.edge(domain, cloud, "hosted_on", fact_id=fact_id, confidence="strong" if served else "weak")
            for tech in tech_ids:
                self.edge(domain, tech, "runs", fact_id=fact_id, confidence="strong" if runs else "weak")
            for cve in cve_ids:
                self.edge(cve, domain, "affects", fact_id=fact_id, confidence="weak")
            for other in domain_ids:
                if other == domain:
                    continue
                if served:
                    self.edge(domain, other, "served_by", fact_id=fact_id, confidence="strong")

        for ip in ip_ids:
            for cve in cve_ids:
                self.edge(cve, ip, "affects", fact_id=fact_id, confidence="weak")
            for tech in tech_ids:
                if "FortiGate" in self.nodes[tech]["label"]:
                    self.edge(ip, tech, "runs", fact_id=fact_id, confidence="strong")

    def anchor_finding(self, *, finding_id: str, severity: str | None, target_text: str) -> None:
        targets: list[str] = []
        for url in URL_PATTERN.findall(target_text)[:1]:
            targets.append(self._clean_url(url) or "")
        no_urls = URL_PATTERN.sub(" ", target_text)
        targets.extend(HOST_PATTERN.findall(no_urls)[:3])
        targets.extend(ip for ip in IP_PATTERN.findall(no_urls)[:2] if _is_valid_ip(ip))
        targets.extend(c.upper() for c in CVE_PATTERN.findall(target_text)[:2])
        targets = list(dict.fromkeys(t for t in targets if t))

        anchored = False
        for target in targets:
            lowered = target.lower()
            for node in self.nodes.values():
                if node["label"].lower() == lowered or (
                    node["type"] in ("domain", "endpoint") and lowered.endswith(node["label"].lower())
                ):
                    self._apply_finding(node, finding_id, severity)
                    anchored = True
        if anchored:
            return
        # fallback: any domain mentioned anywhere in the finding text
        for host in HOST_PATTERN.findall(no_urls):
            if _classify_host(host) == "domain" and f"domain:{host}" in self.nodes:
                self._apply_finding(self.nodes[f"domain:{host}"], finding_id, severity)
                return

    def _apply_finding(self, node: dict, finding_id: str, severity: str | None) -> None:
        if finding_id not in node["finding_ids"]:
            node["finding_ids"].append(finding_id)
        if severity == "negative":
            node["negative_count"] += 1
            return
        if severity and (node["severity"] is None or SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK.get(node["severity"], -1)):
            node["severity"] = severity

    def link_domain_hierarchy(self) -> None:
        children_by_parent: dict[str, list[str]] = defaultdict(list)
        for node in self.nodes.values():
            if node["type"] == "domain" and node.get("parent"):
                children_by_parent[node["parent"]].append(node["id"])

        for parent, children in children_by_parent.items():
            parent_id = f"domain:{parent}"
            if parent_id not in self.nodes and len(children) < 2:
                continue  # single subdomain: hierarchy edge to a synthetic parent is noise
            self.node(parent_id, "domain", parent, synthetic=True)
            for child in children:
                self.edge(child, parent_id, "belongs_to", confidence="strong")

    @staticmethod
    def _clean_url(url: str) -> str | None:
        url = url.rstrip(".,;:)]}>\"'")
        if not url.startswith(("http://", "https://")):
            return None
        return url.split("?")[0].split("#")[0]


# --------------------------------------------------------------------------- renderers


def render_asset_map_json(asset_map: dict) -> str:
    import json

    return json.dumps(asset_map, ensure_ascii=False, indent=2) + "\n"


def render_asset_inventory_md(asset_map: dict) -> str:
    nodes = asset_map["nodes"]
    summary = asset_map["summary"]
    header = asset_map["project"]

    lines: list[str] = []
    lines.append(f"# Attack Surface Inventory — {header['title']}")
    lines.append("")
    lines.append(f"**Project:** `{header['id']}` · **Status:** {header['status']}  ")
    lines.append(
        f"**Assets:** {summary['node_count']} ({_counts_line(summary['counts'])}) · "
        f"**Relations:** {summary['edge_count']}"
    )
    lines.append("")

    def _asset_line(node: dict) -> str:
        severity = node.get("severity")
        marks = []
        if severity:
            marks.append(f"**{severity}**")
        if node.get("negative_count"):
            marks.append(f"{node['negative_count']}× negative-verified")
        detail = (
            node.get("cloud")
            or (f"version {node['version']}" if node.get("version") else None)
            or (f"port {node['port']}" if node.get("port") else None)
            or (f"{node.get('endpoint_class')} endpoint" if node.get("endpoint_class") else None)
        )
        mark_text = f" — {' · '.join(marks)}" if marks else ""
        detail_text = f" ({detail})" if detail else ""
        facts = ", ".join(node["fact_ids"][:4]) or "-"
        intents = ", ".join(node.get("intent_ids", [])[:4]) or "-"
        return f"- `{node['label']}`{detail_text}{mark_text} · facts: {facts} · intents: {intents}"

    def _grouped_section(category: str, title: str) -> None:
        rows = [n for n in nodes if n["category"] == category]
        lines.append(f"## {title} ({len(rows)})")
        lines.append("")
        if not rows:
            lines.append("_None identified._")
            lines.append("")
            return
        grouped: dict[str, list[dict]] = defaultdict(list)
        for node in rows:
            grouped[node.get("group") or ""].append(node)
        for group in sorted(grouped, key=str):
            members = sorted(grouped[group], key=lambda n: n["label"].lower())
            if group:
                lines.append(f"### {group} ({len(members)})")
                lines.append("")
            for node in members:
                lines.append(_asset_line(node))
            lines.append("")

    _grouped_section("cloud", "Cloud & hosting")
    _grouped_section("saas", "Third-party SaaS (holds company data)")
    _grouped_section("domain", "Domains & subdomains")
    _grouped_section("ip", "IP addresses")
    _grouped_section("endpoint", "API & web endpoints")
    _grouped_section("service", "Exposed services (host:port)")
    _grouped_section("tech", "Technology")
    _grouped_section("idp", "Identity providers")
    _grouped_section("cve", "CVEs evaluated")
    _grouped_section("probe", "Test probes (attacker-side infrastructure)")

    lines.append("---")
    lines.append("_Severity = highest finding anchored to the asset. `negative-verified` = tested and confirmed solid. Every asset lists the facts that established it; full fact text and evidence are in the agent report._")
    lines.append("")
    return "\n".join(lines)


def _counts_line(counts: dict[str, int]) -> str:
    return ", ".join(f"{count} {name}" for name, count in counts.items())
