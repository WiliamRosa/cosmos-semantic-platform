"use client";

// The knowledge graph around a process, laid out left to right (systems feed models, models
// feed measures, measures feed metrics, and so on), each box colored by health. Owners appear
// inside the boxes instead of as separate nodes, so the picture stays readable.

import dagre from "@dagrejs/dagre";
import {
  Background,
  Controls,
  type Edge,
  Handle,
  MarkerType,
  type Node,
  type NodeProps,
  Position,
  ReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useEffect, useMemo, useRef, useState } from "react";
import { endpoints, type GraphData, type GraphNode } from "@/lib/api";
import { type Key, useI18n } from "@/lib/i18n";
import { useLoad } from "@/lib/session";
import { Loader, MemberLink, Reasons, STATE_STYLE, StateBadge } from "./ui";

const HIDDEN = new Set(["team", "concept"]);
const WIDTH = 180;
const HEIGHT = 64;

type BoxData = { node: GraphNode; selected: boolean };

function Box({ data }: NodeProps<Node<BoxData>>) {
  const { t } = useI18n();
  const { node, selected } = data;
  const style = STATE_STYLE[node.state ?? "unchecked"];
  const tracked = node.kind === "metric" || node.kind === "measure" || node.kind === "rule";
  return (
    <div
      className={`rounded-lg border-2 bg-white px-3 py-2 text-left shadow-sm dark:bg-slate-900 ${
        tracked || node.kind === "process" ? style.ring : "border-slate-200 dark:border-slate-700"
      } ${selected ? "ring-2 ring-sky-400" : ""}`}
      style={{ width: WIDTH }}
    >
      <Handle type="target" position={Position.Left} className="!bg-slate-300" />
      <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wide text-slate-500">
        {(tracked || node.kind === "process") && (
          <span className={`h-1.5 w-1.5 rounded-full ${style.dot}`} />
        )}
        {t(`kind.${node.kind}` as Key)}
      </div>
      <div className="truncate text-sm font-semibold text-slate-900 dark:text-slate-100" title={node.name}>
        {node.name}
      </div>
      {node.owner && <div className="truncate text-[11px] text-slate-500">{node.owner}</div>}
      <Handle type="source" position={Position.Right} className="!bg-slate-300" />
    </div>
  );
}

const nodeTypes = { box: Box };

function layout(graph: GraphData, selected: string | null) {
  const visible = graph.nodes.filter((n) => !HIDDEN.has(n.kind));
  const ids = new Set(visible.map((n) => n.id));
  const edges = graph.edges.filter((e) => ids.has(e.source) && ids.has(e.target));
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 14, ranksep: 46 });
  g.setDefaultEdgeLabel(() => ({}));
  visible.forEach((n) => g.setNode(n.id, { width: WIDTH, height: HEIGHT }));
  edges.forEach((e) => g.setEdge(e.source, e.target));
  dagre.layout(g);
  const nodes: Node<BoxData>[] = visible.map((n) => {
    const p = g.node(n.id);
    return {
      id: n.id,
      type: "box",
      position: { x: p.x - WIDTH / 2, y: p.y - HEIGHT / 2 },
      data: { node: n, selected: n.id === selected },
    };
  });
  const flowEdges: Edge[] = edges.map((e, i) => ({
    id: `${e.source}->${e.target}-${i}`,
    source: e.source,
    target: e.target,
    markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
    style: { stroke: "#94a3b8" },
  }));
  const { width = 1, height = 1 } = g.graph();
  return { nodes, edges: flowEdges, width, height };
}

export function ProcessGraph({ focus }: { focus?: string }) {
  const { t } = useI18n();
  const loaded = useLoad(() => endpoints.graph(focus), `graph:${focus ?? ""}`);
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <Loader loaded={loaded}>
      {(graph) => (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_300px]">
          <Flow graph={graph} selected={selected} onSelect={setSelected} />
          <aside className="rounded-xl border border-slate-200 bg-white p-4 text-sm dark:border-slate-800 dark:bg-slate-900">
            {selected ? (
              <NodePanel node={graph.nodes.find((n) => n.id === selected)!} />
            ) : (
              <p className="text-slate-600 dark:text-slate-400">{t("processes.legend")}</p>
            )}
          </aside>
        </div>
      )}
    </Loader>
  );
}

function Flow({
  graph,
  selected,
  onSelect,
}: {
  graph: GraphData;
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const { nodes, edges, width, height } = useMemo(
    () => layout(graph, selected),
    [graph, selected],
  );
  // The box follows the drawing's shape, so a wide, shallow process is not lost in empty
  // space: its height comes from the width it actually has, between 300 and 640 pixels.
  const box = useRef<HTMLDivElement>(null);
  const [boxWidth, setBoxWidth] = useState(0);
  useEffect(() => {
    const element = box.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setBoxWidth(entry.contentRect.width));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const boxHeight = boxWidth
    ? Math.min(640, Math.max(300, (boxWidth * (height + 80)) / (width + 80)))
    : 480;
  return (
    <div
      ref={box}
      className="min-w-0 rounded-xl border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900"
      style={{ height: boxHeight }}
    >
    <ReactFlow
      key={Math.round(boxHeight)} // refit when the box changes size
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      fitView
      fitViewOptions={{ padding: 0.04, maxZoom: 1.1 }}
      minZoom={0.2}
      nodesDraggable={false}
      nodesConnectable={false}
      onNodeClick={(_, node) => onSelect(node.id)}
    >
      <Background gap={20} color="#e2e8f0" />
      <Controls showInteractive={false} />
    </ReactFlow>
    </div>
  );
}

function NodePanel({ node }: { node: GraphNode }) {
  const { t, count } = useI18n();
  const tracked = ["metric", "measure", "rule"].includes(node.kind);
  return (
    <div className="space-y-3">
      <div>
        <div className="text-xs font-medium uppercase tracking-wide text-slate-500">
          {t(`kind.${node.kind}` as Key)}
        </div>
        <div className="text-base font-semibold">{node.name}</div>
      </div>
      {(tracked || node.kind === "process") && (
        <div>
          <StateBadge state={node.state} />
          <Reasons reasons={node.reasons} />
        </div>
      )}
      {node.description && <p className="text-slate-700 dark:text-slate-300">{node.description}</p>}
      <div className="text-slate-600 dark:text-slate-400">
        {t("common.owner")}: {node.owner ?? t("common.noOwner")}
      </div>
      {typeof node.queries === "number" && (
        <div className="text-slate-600 dark:text-slate-400">
          {count(node.queries, "common.query", "common.queries")}
        </div>
      )}
      {(tracked || ["process", "system"].includes(node.kind)) && (
        <MemberLink id={node.id}>{t("common.open")} →</MemberLink>
      )}
    </div>
  );
}
