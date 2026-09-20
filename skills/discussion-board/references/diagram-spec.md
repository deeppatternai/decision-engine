# Diagram Board Specification

Read this reference only when assembling a `diagram` board. You, the agent, supply user content;
the server computes every coordinate and renders the interactive board. The user may drag nodes
and Submit the revised board. The board UI places server-computed positions, permits free dragging,
and re-fetches on 复位; no layout IP reaches the skill.

## Supported families

- **Node-and-edge:** `flow`, `mindmap`, `tree`, `cycle`, `concept-map`, `org`, `sequence`,
  `state-machine`.
- **Structured:** `entity-relationship` (ER), `truth-table`, `decision-table`,
  `decision-matrix`. These render real structured content inside nodes: attribute rows, a truth
  grid, a rule table, or a weighted score matrix.
- **Axis, band, and chrome:** `swot`, `affinity`, `canvas`, `funnel`, `journey`, `swimlane`,
  `story-map`, `gantt`, `venn`, `fishbone`, `quadrants`, `timeline`. Their server-rendered
  backgrounds include, respectively, a named 2×2 grid; titled cluster regions; the 9-pane BMC
  template; a funnel or pyramid silhouette; a stage×track grid with an emotion curve; tinted lane
  bands; a two-level activity→task×release backbone; duration bars on tracks under a time ruler;
  overlapping set circles; an effect spine with category bones; a scatter axis-cross with scale
  ticks; and point events on tracks under a time ruler. Each retains its axis, band, lane, set, or
  category labels as described below.

## Base spec

Drop `columns` from the kanban example in SKILL.md and pass a `diagram` object. `layout` selects
the family; `nodes` and `edges` are user content only:

```json
{"title": "登录流程",
 "diagram": {
   "layout": "flow",
   "nodes": [
     {"id": "n1", "text": "开始", "shape": "round"},
     {"id": "n2", "text": "输入校验"},
     {"id": "n3", "text": "有效?", "shape": "diamond"},
     {"id": "n4", "text": "进入首页"},
     {"id": "n5", "text": "报错"}],
   "edges": [
     {"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"},
     {"from": "n3", "to": "n4"}, {"from": "n3", "to": "n5"}]},
 "notes": "", "comments": []}
```

Each node needs a unique `id` and a `text` label; `shape` is optional (`round` / `diamond`,
otherwise a plain box). `edges` carry `{from, to}` node ids and an optional `label`: the
relation word, branch, condition, or message represented by the line (for example,
`{"from":"n3","to":"n5","label":"否"}`). The server places the caption on the edge's own
shape (line midpoint, elbow stub, or arc apex), and it rides or hides with the edge when the user
drags. Families that draw no line carry no caption. Do NOT pass coordinates, sizes, or any layout
hint; the server owns the geometry.

## Appearance

A node may carry `color`: one of `start` (blue), `good` (green), `bad` (red), `warn` (amber),
`neutral` (default teal), or `black`. An edge's line inherits its SOURCE (`from`) node's colour by
default, so one node's outgoing lines read as one colour. Pass `color` on the edge to override
that, and `dash: true` for a dashed line. Arrowheads and entity-relationship crow's-foot symbols
take the line's colour too. Example:
`{"from":"n3","to":"n5","label":"否","color":"bad","dash":true}`.
On a `timeline`, a dependency that runs BACKWARD in time (`from.t > to.t`, a predecessor scheduled
after its successor) is auto-flagged as a red dashed conflict line, overriding its own colour and
dash; do not set this yourself.

## Relation families

These extra per-node and per-edge fields remain user content. The server computes all coordinates
and message order.

- `sequence`: each node is a participant lifeline: `col:` its column order (0-based, left to
  right). Each edge is a message: `order:` its time order down the page (0-based), and `kind:`
  its line style `"sync"` (solid, default), `"async"` (open), or `"return"` (dashed). A message
  from a participant to itself (`from == to`) draws a self-call loop. `label` is the message text
  and renders larger than other families' edge labels.
- `state-machine`: each node is a state: `marker:` a pseudo-state `"start"` (entry disc) or
  `"final"` (ring and inner disc); `shape: "diamond"` denotes a decision/choice state. Each
  edge is a transition, with `label` as its trigger or condition (`"启动"` / `"是"`). A
  `from == to` edge is a self-transition (lifted top arc).

## Structured families

These carry extra per-node content; the server computes coordinates and derived values.

- `entity-relationship`: each node is an entity. Add `attrs: ["id","name","email"]` for
  attribute rows rendered inside its box. `edges` are the relationships.
- `truth-table`: each node is a boolean function: `inputs: ["A","B"]`, `outputs: ["Q"]`,
  and `values: "0001"`. ONE output uses a 2ⁿ-character string of `0`/`1`/`x`; MULTIPLE outputs
  use an array of one such string per output. The 2ⁿ input rows are auto-enumerated (≤ 10 inputs).
- `decision-table`: each node is a rule set: `rules: ["R1","R2","R3"]`,
  `conditions: [{"n":"已收货","c":["Y","N","Y"]}]`, and
  `actions: [{"n":"允许退款","c":["✓","✓",""]}]` (one `c` entry per rule).
- `decision-matrix`: options scored against criteria. Put criteria on top-level `diagram.axes`:
  `{"cols":["成本","性能","易用"],"weights":[3,2,2]}`. Each node is an option with
  `cells: ["8","6","7"]` (one per criterion). The server derives the weighted Total column
  and highlights the winning row. Do NOT compute or pass totals.

## Axis and band families

These draw a labelled background the cards sort INTO. A card carries its group index; group
titles ride `diagram.axes`. The server draws every band, cell, axis, and curve.

- `swot`: a named 2×2. Each node `cluster: 0..3` (0=TL, 1=TR, 2=BL, 3=BR);
  `axes.clusters` holds the 4 bin titles (default 优势/劣势/机会/威胁), with `axes.bandCols` and
  `axes.bandRows` for the two axis labels (for example `["内部","外部"]` /
  `["积极","消极"]`). Relabel to Eisenhower or another 2×2.
- `affinity`: free theme clustering. Each node `cluster: 0-based` theme index (omit for 未分类
  tray); `axes.clusters` holds the theme titles. Cards regroup by drag.
- `canvas`: the 9-pane Business-Model or Lean canvas. Each node `cluster: 0..8` (pane index);
  `axes.clusters` holds the 9 pane titles (default BMC; relabel for Lean Canvas).
- `funnel`: stacked stages. Each node `tier: 0-based` band; `axes.tiers` holds the stage titles;
  `axes.orientation: "funnel"` (wide top) or `"pyramid"` (wide bottom).
- `journey`: a 2-D stage×track grid and emotion curve. Each node `col:` stage and `lane:` track
  (its cell); `axes.cols` holds stage titles, `axes.lanes` track titles, and `axes.emotion` a
  per-stage sentiment array in `[-1,+1]` (one per column) that draws the curve.
- `swimlane`: lane bands. Each node `lane: 0-based` band; `axes.lanes` holds lane titles;
  `axes.laneAxis: "row"` (horizontal bands, default) or `"col"` (vertical bands).
- `story-map`: a task×release grid under a two-level activity backbone. Each node `col:` task
  column and `lane:` release row (its cell); `axes.cols` holds task titles, `axes.lanes` release
  titles, `axes.activities` activity titles, and `axes.colAct` a per-column activity index array
  (one per task column) that draws the top-level spanning band above the tasks.
- `gantt`: duration bars on tracks under a time ruler. Each node `lane:` track, `t:` start, and
  `t1:` end (a bar; omit `t1` for a milestone diamond); `axes.lanes` holds track titles,
  `axes.bottom` the time-axis caption, and optional `axes.ticks: [{"t":0,"label":"1月"}]`
  supplies custom date ticks (else nice numbers). Bars and milestones sit at `t` on the shared
  scale.
- `venn`: overlapping set circles. `axes.sets` holds the 2–3 set titles; each node `region:`
  an array of set indices it belongs to (for example `[0]` for one set, `[0,1,2]` for the triple
  overlap; omit for an external item outside every circle).
- `fishbone`: an Ishikawa/cause-effect diagram. `axes.effect` holds the effect/problem title
  (the head box), `axes.cats` the category-bone titles; each node `cat:` its category index
  (the cause snaps onto that bone; omit for 未分类 tray).
- `quadrants`: a 2×2/scatter quadrant. `axes.top`, `axes.bottom`, `axes.left`, and
  `axes.right` hold the four edge direction titles (x→right, y→top aliases too); `axes.q`
  holds the four corner labels `[top-right, top-left, bottom-left, bottom-right]`. Each node
  `value: [vx,vy]` is a numeric point (the card sits at that value with axis scale ticks and a
  mean line); omit `value` on every node for a categorical 2×2 across which cards spread.
- `timeline`: point events on tracks under a time ruler. Each node `lane:` track and `t:` event
  time; `axes.lanes` holds track titles, `axes.bottom` the time-axis caption, and optional
  `axes.ticks: [{"t":0,"label":"1月"}]` supplies custom date ticks (else nice numbers).
  Events sit centred at `t` on the shared scale.
