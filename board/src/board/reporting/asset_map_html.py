"""Self-contained attack-surface report (single HTML file), lists first.

The primary view is a categorized, searchable inventory of every asset with
provenance on demand; the node graph is a secondary tab for orientation. The
file inlines cytoscape + dagre from the vendored UX libraries and works offline.
"""

from __future__ import annotations

import json
from importlib import resources

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>__TITLE__</title>
<style>
  * { box-sizing: border-box; margin: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }
  header { padding: 10px 16px; background: #1e293b; border-bottom: 1px solid #334155; display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
  header h1 { font-size: 15px; font-weight: 600; color: #f1f5f9; white-space: nowrap; }
  header .meta { font-size: 11px; color: #94a3b8; }
  #toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; font-size: 12px; margin-left: auto; }
  #toolbar input[type=search] { background: #0f172a; border: 1px solid #334155; color: #e2e8f0; border-radius: 6px; padding: 5px 10px; width: 240px; }
  #toolbar button { background: #334155; border: none; color: #e2e8f0; border-radius: 6px; padding: 5px 12px; cursor: pointer; }
  #toolbar button:hover { background: #475569; }
  .tabs { display: flex; gap: 2px; padding: 0 16px; background: #1e293b; border-bottom: 1px solid #334155; }
  .tabs button { background: none; border: none; color: #94a3b8; padding: 8px 16px; font-size: 13px; cursor: pointer; border-bottom: 2px solid transparent; }
  .tabs button.active { color: #f1f5f9; border-bottom-color: #6366f1; }
  main { flex: 1; display: flex; min-height: 0; }
  #inventory { flex: 1; display: flex; min-height: 0; }
  #sidebar { width: 250px; border-right: 1px solid #334155; background: #1e293b; overflow-y: auto; padding: 10px 0; flex-shrink: 0; }
  #sidebar .cat { display: flex; justify-content: space-between; align-items: center; padding: 7px 16px; font-size: 13px; color: #cbd5e1; cursor: pointer; border-left: 3px solid transparent; }
  #sidebar .cat:hover { background: #273449; }
  #sidebar .cat.active { background: #273449; border-left-color: #6366f1; color: #f1f5f9; }
  #sidebar .cat .n { color: #64748b; font-size: 11px; }
  #sidebar .all { border-bottom: 1px solid #334155; margin-bottom: 6px; padding-bottom: 6px; }
  #listing { flex: 1; overflow-y: auto; padding: 14px 18px; }
  .catblock { margin-bottom: 28px; }
  .catblock h2 { font-size: 14px; color: #f1f5f9; margin-bottom: 8px; }
  .grouphead { font-size: 12px; color: #94a3b8; text-transform: uppercase; letter-spacing: .06em; margin: 14px 0 4px; border-bottom: 1px solid #334155; padding-bottom: 3px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: #64748b; font-size: 11px; text-transform: uppercase; letter-spacing: .06em; padding: 4px 8px; border-bottom: 1px solid #334155; }
  td { padding: 5px 8px; border-bottom: 1px solid #1e293b; vertical-align: top; }
  tr.asset { cursor: pointer; }
  tr.asset:hover { background: #1e293b; }
  td.mono, .mono { font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: 12px; word-break: break-all; }
  .chip { display: inline-block; padding: 0 7px; border-radius: 999px; font-size: 10px; font-weight: 700; text-transform: uppercase; }
  .sev-critical { background: #ef444422; color: #ef4444; } .sev-high { background: #f8717122; color: #f87171; }
  .sev-medium { background: #fb923c22; color: #fb923c; } .sev-low { background: #facc1522; color: #facc15; }
  .sev-info { background: #94a3b822; color: #94a3b8; } .sev-neg { background: #34d39922; color: #34d399; }
  .dim { color: #64748b; font-size: 12px; }
  tr.prov td { background: #0b1220; padding: 10px 12px; }
  tr.prov .fact { margin-bottom: 8px; }
  tr.prov .fact .fid { color: #38bdf8; font-family: ui-monospace, Menlo, monospace; font-size: 11px; }
  tr.prov .fact .ftext { white-space: pre-wrap; color: #cbd5e1; font-size: 12px; max-height: 220px; overflow-y: auto; margin-top: 3px; border-left: 2px solid #334155; padding-left: 8px; }
  tr.prov .ev { color: #64748b; font-family: ui-monospace, Menlo, monospace; font-size: 11px; }
  #graphwrap { flex: 1; display: none; position: relative; }
  #cy { position: absolute; inset: 0; }
  #graphbar { position: absolute; top: 10px; left: 12px; z-index: 5; display: flex; gap: 8px; align-items: center; background: rgba(30,41,59,.9); padding: 6px 10px; border-radius: 8px; border: 1px solid #334155; font-size: 12px; }
  #graphbar select, #graphbar label { color: #cbd5e1; }
  #graphbar select { background: #0f172a; border: 1px solid #334155; color: #e2e8f0; border-radius: 6px; padding: 3px; }
  #legend { position: absolute; bottom: 10px; left: 12px; background: rgba(30,41,59,.9); border: 1px solid #334155; border-radius: 8px; padding: 8px 10px; font-size: 11px; display: flex; gap: 10px; flex-wrap: wrap; max-width: 70%; }
  #legend .dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; margin-right: 4px; }
  #noresults { color: #64748b; padding: 30px; text-align: center; display: none; }
</style>
<script>__CYTOSCAPE__</script>
<script>__DAGRE__</script>
<script>__CYTOSCAPE_DAGRE__</script>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <span class="meta">__META__</span>
  <div id="toolbar">
    <input type="search" id="search" placeholder="Search all assets…">
    <button id="csv">Download CSV</button>
  </div>
</header>
<div class="tabs">
  <button id="tab-inv" class="active">Inventory</button>
  <button id="tab-graph">Graph</button>
</div>
<main>
  <div id="inventory">
    <nav id="sidebar"></nav>
    <div id="listing"><div id="noresults">No assets match the search.</div></div>
  </div>
  <div id="graphwrap">
    <div id="graphbar">
      <select id="layout">
        <option value="dagre-tb">Hierarchical</option>
        <option value="dagre-lr">Flow →</option>
        <option value="cose">Organic</option>
        <option value="circle">Ring</option>
        <option value="grid">Grid</option>
      </select>
      <label><input type="checkbox" id="probes"> probes</label>
      <label><input type="checkbox" id="sevOnly"> findings only</label>
      <button id="fit">Fit</button>
    </div>
    <div id="cy"></div>
    <div id="legend"></div>
  </div>
</main>
<script>
const DATA = __DATA__;
const CATS = [
  ['cloud', 'Cloud & hosting'],
  ['saas', 'Third-party SaaS'],
  ['domain', 'Domains & subdomains'],
  ['ip', 'IP addresses'],
  ['endpoint', 'API & web endpoints'],
  ['service', 'Exposed services'],
  ['tech', 'Technology'],
  ['idp', 'Identity providers'],
  ['cve', 'CVEs evaluated'],
  ['probe', 'Test probes'],
];
const nodesByCat = {};
DATA.nodes.forEach(n => { (nodesByCat[n.category] = nodesByCat[n.category] || []).push(n); });
const presentCats = CATS.filter(([k]) => nodesByCat[k] && nodesByCat[k].length);
let activeCat = 'all';
let query = '';

function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function detailOf(n) {
  if (n.cloud) return n.cloud;
  if (n.version) return 'version ' + n.version;
  if (n.port) return 'port ' + n.port;
  if (n.endpoint_class) return n.endpoint_class + ' endpoint';
  if (n.type === 'ip') { const c = (DATA.edges.find(e => e.source === 'ip:' + n.label && e.type === 'hosted_on')); return c ? 'on ' + c.target.replace('cloud:', '') : ''; }
  return '';
}
function sevChip(n) {
  let html = '';
  if (n.severity) html += '<span class="chip sev-' + n.severity + '">' + n.severity + '</span> ';
  if (n.negative_count > 0) html += '<span class="chip sev-neg">' + n.negative_count + '&check; solid</span>';
  return html || '<span class="dim">—</span>';
}
function matches(n) {
  return !query || n.label.toLowerCase().includes(query)
    || (n.group || '').toLowerCase().includes(query)
    || detailOf(n).toLowerCase().includes(query);
}

const sidebar = document.getElementById('sidebar');
function catLabel(key) { const c = CATS.find(([k]) => k === key); return c ? c[1] : key; }
function renderSidebar() {
  const total = Object.values(nodesByCat).reduce((a, b) => a + b.length, 0);
  let html = '<div class="cat all' + (activeCat === 'all' ? ' active' : '') + '" data-cat="all"><span>All assets</span><span class="n">' + total + '</span></div>';
  presentCats.forEach(([key]) => {
    html += '<div class="cat' + (activeCat === key ? ' active' : '') + '" data-cat="' + key + '"><span>' + catLabel(key) + '</span><span class="n">' + nodesByCat[key].length + '</span></div>';
  });
  sidebar.innerHTML = html;
  sidebar.querySelectorAll('.cat').forEach(el => el.addEventListener('click', () => {
    activeCat = el.dataset.cat; renderSidebar(); renderListing();
  }));
}

function provRow(n) {
  const facts = (n.fact_ids || []).map(fid => DATA.facts[fid]).filter(Boolean);
  let html = '';
  if ((n.finding_ids || []).length) html += '<div class="dim" style="margin-bottom:6px">Findings anchored here: ' + n.finding_ids.map(f => '<span class="mono">' + esc(f) + '</span>').join(', ') + '</div>';
  if ((n.intent_ids || []).length) {
    const chips = n.intent_ids.map(iid => {
      const it = DATA.intents[iid];
      const tip = it ? esc((it.description || '').slice(0, 160)) : '';
      return '<span class="mono" style="cursor:help' + (it && it.open ? ';color:#fbbf24' : '') + '" title="' + tip + '">' + esc(iid) + '</span>';
    }).join(', ');
    html += '<div class="dim" style="margin-bottom:6px">Intents touching this asset (' + n.intent_ids.length + '; hover for description, amber = still open): ' + chips + '</div>';
  }
  if (facts.length) {
    html += '<div class="dim" style="margin-bottom:4px">Established by ' + facts.length + ' fact' + (facts.length > 1 ? 's' : '') + ':</div>';
    facts.forEach((f, i) => {
      const ev = (f.evidence || []).map(e => e.id).join(' ');
      const via = f.intent_id ? ' · via ' + esc(f.intent_id) : '';
      html += '<div class="fact"><span class="fid">' + esc(n.fact_ids[i]) + via + (ev ? ' · evidence: ' + esc(ev) : '') + '</span><div class="ftext">' + esc(f.text.slice(0, 900)) + (f.text.length > 900 ? '…' : '') + '</div></div>';
    });
  } else if (n.synthetic) {
    html += '<div class="dim">Synthetic grouping node (parent domain).</div>';
  } else {
    html += '<div class="dim">No fact provenance recorded.</div>';
  }
  return html;
}

function renderListing() {
  const listing = document.getElementById('listing');
  const cats = activeCat === 'all' ? presentCats : presentCats.filter(([k]) => k === activeCat);
  let html = '';
  let shown = 0;
  cats.forEach(([key]) => {
    const rows = nodesByCat[key].filter(matches);
    shown += rows.length;
    if (!rows.length) return;
    html += '<div class="catblock"><h2>' + catLabel(key) + ' (' + rows.length + ')</h2><table><thead><tr><th style="width:32%">Asset</th><th style="width:20%">Detail</th><th style="width:16%">Severity</th><th>Findings / Facts / Intents</th></tr></thead><tbody>';
    const grouped = {};
    rows.forEach(n => { const g = n.group || ''; (grouped[g] = grouped[g] || []).push(n); });
    Object.keys(grouped).sort().forEach(g => {
      if (g && (activeCat === 'all' || grouped[g].length > 1 || key === 'domain' || key === 'saas' || key === 'cloud')) {
        html += '<tr><td colspan="4" class="grouphead">' + esc(g) + ' (' + grouped[g].length + ')</td></tr>';
      }
      grouped[g].sort((a, b) => a.label.toLowerCase().localeCompare(b.label.toLowerCase())).forEach(n => {
        html += '<tr class="asset" data-node="' + esc(n.id) + '"><td class="mono">' + esc(n.label) + '</td><td class="dim">' + esc(detailOf(n)) + '</td><td>' + sevChip(n) + '</td><td class="dim">' + (n.finding_ids || []).length + ' / ' + (n.fact_ids || []).length + ' / ' + (n.intent_ids || []).length + '</td></tr>';
      });
    });
    html += '</tbody></table></div>';
  });
  document.getElementById('noresults').style.display = shown ? 'none' : 'block';
  listing.innerHTML = html;
  listing.querySelectorAll('tr.asset').forEach(tr => tr.addEventListener('click', () => {
    const next = tr.nextElementSibling;
    if (next && next.classList.contains('prov')) { next.remove(); return; }
    const n = DATA.nodes.find(x => x.id === tr.dataset.node);
    const row = document.createElement('tr');
    row.className = 'prov';
    row.innerHTML = '<td colspan="4">' + provRow(n) + '</td>';
    tr.after(row);
  }));
}

document.getElementById('search').addEventListener('input', ev => {
  query = ev.target.value.trim().toLowerCase(); renderListing();
});

document.getElementById('csv').addEventListener('click', () => {
  const rows = [['category', 'group', 'type', 'asset', 'detail', 'severity', 'negative_verified', 'findings', 'facts', 'intents']];
  DATA.nodes.filter(matches).forEach(n => rows.push([
    n.category, n.group || '', n.type, n.label, detailOf(n), n.severity || '',
    n.negative_count || 0, (n.finding_ids || []).join(' '), (n.fact_ids || []).join(' '), (n.intent_ids || []).join(' '),
  ]));
  const csv = rows.map(r => r.map(c => '"' + String(c).replace(/"/g, '""') + '"').join(',')).join('\\r\\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([csv], {type: 'text/csv'}));
  a.download = 'strikebox-' + DATA.project.id + '-assets.csv';
  a.click();
});

// tabs
const tabInv = document.getElementById('tab-inv'), tabGraph = document.getElementById('tab-graph');
tabInv.addEventListener('click', () => { tabInv.classList.add('active'); tabGraph.classList.remove('active'); document.getElementById('inventory').style.display = 'flex'; document.getElementById('graphwrap').style.display = 'none'; });
tabGraph.addEventListener('click', () => { tabGraph.classList.add('active'); tabInv.classList.remove('active'); document.getElementById('inventory').style.display = 'none'; document.getElementById('graphwrap').style.display = 'block'; cy.resize(); cy.fit(undefined, 40); });

renderSidebar();
renderListing();

// ---------------- graph tab ----------------
const COLORS = {domain:'#6366f1', endpoint:'#0ea5e9', ip:'#f59e0b', service:'#fb923c', tech:'#a78bfa', cloud:'#34d399', idp:'#f472b6', cve:'#f87171', probe:'#94a3b8'};
const SEVC = {critical:'#ef4444', high:'#f87171', medium:'#fb923c', low:'#facc15', info:'#94a3b8'};
function shortLabel(n) {
  let l = n.label;
  if (n.type === 'endpoint') { try { const u = new URL(l); l = u.host.split('.')[0] + u.pathname.slice(0, 18); } catch (e) {} }
  return l.length > 28 ? l.slice(0, 27) + '…' : l;
}
const elements = [
  ...DATA.nodes.map(n => ({data: {id: n.id, label: shortLabel(n), ntype: n.type, color: COLORS[n.type] || '#64748b', borderWidth: n.severity ? 3 : 0, borderColor: SEVC[n.severity] || '#f87171'}})),
  ...DATA.edges.map((e, i) => ({data: {id: 'e' + i, source: e.source, target: e.target, etype: e.type}})),
];
const cy = cytoscape({
  container: document.getElementById('cy'),
  elements,
  style: [
    {selector: 'node', style: {label: 'data(label)', 'background-color': 'data(color)', color: '#cbd5e1', 'font-size': 8, 'text-valign': 'bottom', 'text-margin-y': 4, 'min-zoomed-font-size': 6, width: 18, height: 18, 'border-width': 'data(borderWidth)', 'border-color': 'data(borderColor)'}},
    {selector: 'node[ntype="cve"], node[ntype="cloud"], node[ntype="idp"]', style: {shape: 'round-diamond'}},
    {selector: 'edge', style: {width: 1, 'line-color': '#334155', 'target-arrow-shape': 'vee', 'arrow-scale': 0.5, 'curve-style': 'bezier', opacity: 0.7}},
    {selector: 'edge[etype="belongs_to"], edge[etype="associated"]', style: {'line-style': 'dashed', opacity: 0.35}},
  ],
  layout: {name: 'dagre', rankDir: 'TB', nodeSep: 14, edgeSep: 4, rankSep: 40},
});
const legend = document.getElementById('legend');
Object.entries(COLORS).forEach(([t, c]) => {
  const key = document.createElement('span');
  key.innerHTML = '<span class="dot" style="background:' + c + '"></span>' + t;
  legend.appendChild(key);
});
function graphFilters() {
  const showProbes = document.getElementById('probes').checked;
  const sevOnly = document.getElementById('sevOnly').checked;
  cy.nodes().forEach(n => {
    const raw = DATA.nodes.find(x => x.id === n.id());
    let show = true;
    if (raw.type === 'probe' && !showProbes) show = false;
    if (sevOnly && !raw.severity && !(raw.negative_count > 0)) show = false;
    if (query && !raw.label.toLowerCase().includes(query)) show = false;
    n.style('display', show ? 'element' : 'none');
  });
  cy.edges().forEach(e => {
    const visible = e.source().style('display') !== 'none' && e.target().style('display') !== 'none';
    e.style('display', visible ? 'element' : 'none');
  });
}
document.getElementById('probes').addEventListener('change', graphFilters);
document.getElementById('sevOnly').addEventListener('change', graphFilters);
document.getElementById('search').addEventListener('input', graphFilters);
document.getElementById('layout').addEventListener('change', ev => {
  const name = ev.target.value.startsWith('dagre') ? 'dagre' : ev.target.value.split('-')[0];
  const opts = name === 'dagre' ? {name, rankDir: ev.target.value.endsWith('lr') ? 'LR' : 'TB', nodeSep: 14, rankSep: 40} : {name};
  cy.layout(opts).run();
});
document.getElementById('fit').addEventListener('click', () => cy.fit(undefined, 40));
</script>
</body>
</html>
"""


def render_asset_map_html(asset_map: dict) -> str:
    vendor = resources.files("board.server.static").joinpath("vendor")
    libraries = {
        "__CYTOSCAPE__": vendor.joinpath("cytoscape.min.js").read_text(encoding="utf-8"),
        "__DAGRE__": vendor.joinpath("dagre.min.js").read_text(encoding="utf-8"),
        "__CYTOSCAPE_DAGRE__": vendor.joinpath("cytoscape-dagre.js").read_text(encoding="utf-8"),
    }
    project = asset_map["project"]
    summary = asset_map["summary"]
    html = _TEMPLATE
    html = html.replace("__TITLE__", f"Attack Surface — {project['title']} ({project['id']})")
    html = html.replace(
        "__META__",
        f"{project['id']} · {project['status']} · {summary['node_count']} assets · "
        f"{summary['edge_count']} relations · generated from the Strikebox blackboard",
    )
    for token, library in libraries.items():
        html = html.replace(token, library.replace("</script>", "<\\/script>"))
    payload = json.dumps(asset_map, ensure_ascii=False).replace("</", "<\\/")
    html = html.replace("__DATA__", payload)
    return html
