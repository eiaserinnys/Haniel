// DependencyGraph — SVG-based DAG visualisation of service dependencies.
//
// Layout strategy:
//   - Assign each node a layer: 0 for nodes with no dependencies,
//     max(dep layers) + 1 for nodes that depend on others.
//   - Within each layer, nodes are sorted alphabetically and spaced evenly.
//   - Edges are cubic bezier curves pointing FROM dependency TO dependent.

import type { DependencyInfo } from '@/lib/types'

const NODE_W = 134
const NODE_H = 36
const LAYER_GAP = 190
const NODE_GAP = 56
const PAD = 20

interface NodePosition {
  x: number
  y: number
}

function computeLayers(
  graph: Record<string, DependencyInfo>,
): Map<string, number> {
  const layers = new Map<string, number>()
  for (const node of Object.keys(graph)) {
    layers.set(node, 0)
  }

  // Propagate: each node's layer = max(dep layers) + 1
  let changed = true
  while (changed) {
    changed = false
    for (const [node, info] of Object.entries(graph)) {
      for (const dep of info.dependencies) {
        if (!layers.has(dep)) continue
        const needed = (layers.get(dep) ?? 0) + 1
        if (needed > (layers.get(node) ?? 0)) {
          layers.set(node, needed)
          changed = true
        }
      }
    }
  }

  return layers
}

function computePositions(
  graph: Record<string, DependencyInfo>,
): Map<string, NodePosition> {
  const layerMap = computeLayers(graph)

  // Group by layer
  const byLayer = new Map<number, string[]>()
  for (const [node, layer] of layerMap) {
    if (!byLayer.has(layer)) byLayer.set(layer, [])
    byLayer.get(layer)!.push(node)
  }

  // Sort nodes within each layer alphabetically for deterministic output
  for (const nodes of byLayer.values()) {
    nodes.sort()
  }

  const maxPerLayer = Math.max(...Array.from(byLayer.values()).map(v => v.length))
  const totalContentH = maxPerLayer * NODE_H + (maxPerLayer - 1) * NODE_GAP

  const positions = new Map<string, NodePosition>()
  for (const [layer, layerNodes] of byLayer) {
    const layerH = layerNodes.length * NODE_H + (layerNodes.length - 1) * NODE_GAP
    const startY = PAD + (totalContentH - layerH) / 2
    layerNodes.forEach((node, i) => {
      positions.set(node, {
        x: PAD + layer * LAYER_GAP,
        y: startY + i * (NODE_H + NODE_GAP),
      })
    })
  }

  return positions
}

interface DependencyGraphProps {
  graph: Record<string, DependencyInfo>
}

export function DependencyGraph({ graph }: DependencyGraphProps) {
  const nodes = Object.keys(graph)

  if (nodes.length === 0) {
    return (
      <p className="text-sm text-zinc-500 text-center py-4">
        서비스가 없습니다.
      </p>
    )
  }

  const positions = computePositions(graph)
  const layerMap = computeLayers(graph)
  const maxLayer = Math.max(...layerMap.values())

  const perLayerCounts = new Map<number, number>()
  for (const node of nodes) {
    const l = layerMap.get(node) ?? 0
    perLayerCounts.set(l, (perLayerCounts.get(l) ?? 0) + 1)
  }
  const realMaxPerLayer = Math.max(...perLayerCounts.values())

  const svgW = PAD * 2 + (maxLayer + 1) * LAYER_GAP
  const svgH = PAD * 2 + realMaxPerLayer * NODE_H + (realMaxPerLayer - 1) * NODE_GAP

  // Build edges: from dependency → to dependent
  const edges: Array<{ from: string; to: string }> = []
  for (const node of nodes) {
    for (const dep of graph[node].dependencies) {
      if (positions.has(dep)) {
        edges.push({ from: dep, to: node })
      }
    }
  }

  return (
    <div className="overflow-x-auto">
      <svg
        width={svgW}
        height={svgH}
        viewBox={`0 0 ${svgW} ${svgH}`}
        style={{ fontFamily: 'ui-monospace, SFMono-Regular, monospace' }}
      >
        <defs>
          <marker
            id="dag-arrow"
            markerWidth="8"
            markerHeight="8"
            refX="7"
            refY="3"
            orient="auto"
          >
            <path d="M0,0 L0,6 L8,3 z" fill="#52525b" />
          </marker>
        </defs>

        {/* Edges */}
        {edges.map(({ from, to }) => {
          const f = positions.get(from)!
          const t = positions.get(to)!
          const x1 = f.x + NODE_W
          const y1 = f.y + NODE_H / 2
          const x2 = t.x - 5
          const y2 = t.y + NODE_H / 2
          const mx = (x1 + x2) / 2
          return (
            <path
              key={`${from}→${to}`}
              d={`M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`}
              fill="none"
              stroke="#3f3f46"
              strokeWidth="1.5"
              markerEnd="url(#dag-arrow)"
            />
          )
        })}

        {/* Nodes */}
        {nodes.map(node => {
          const pos = positions.get(node)!
          const label = node.length > 16 ? node.slice(0, 14) + '…' : node
          return (
            <g key={node}>
              <rect
                x={pos.x}
                y={pos.y}
                width={NODE_W}
                height={NODE_H}
                rx="6"
                fill="#18181b"
                stroke="#3f3f46"
                strokeWidth="1"
              />
              <text
                x={pos.x + NODE_W / 2}
                y={pos.y + NODE_H / 2 + 4}
                textAnchor="middle"
                fontSize="11"
                fill="#a1a1aa"
              >
                {label}
              </text>
            </g>
          )
        })}
      </svg>
    </div>
  )
}
