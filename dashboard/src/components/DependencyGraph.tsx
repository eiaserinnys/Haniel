// DependencyGraph — SVG DAG of service dependencies (a list when the content is narrow).
//
// Layout: a node's column is max(dependency columns) + 1; edges run from a dependency to its dependent.

import type { DependencyInfo } from '@/lib/types'

const NODE_W = 216
const NODE_H = 40
const GAP_X = 96
const GAP_Y = 16
const LABEL_MAX = 24
const ARROW = 7

function columnOf(graph: Record<string, DependencyInfo>): Map<string, number> {
  const columns = new Map<string, number>()
  const visit = (node: string, path: Set<string>): number => {
    const known = columns.get(node)
    if (known !== undefined) return known
    if (path.has(node)) return 0
    path.add(node)
    const deps = (graph[node]?.dependencies ?? []).filter((d) => d in graph)
    const column = deps.length ? Math.max(...deps.map((d) => visit(d, path))) + 1 : 0
    columns.set(node, column)
    return column
  }
  Object.keys(graph).forEach((node) => visit(node, new Set()))
  return columns
}

const shortName = (name: string) => (name.length > LABEL_MAX ? `${name.slice(0, LABEL_MAX - 1)}…` : name)

export function DependencyGraph({ graph }: { graph: Record<string, DependencyInfo> }) {
  const names = Object.keys(graph)
  const columns = columnOf(graph)
  const lanes: string[][] = []
  names.forEach((name) => {
    const c = columns.get(name) ?? 0
    ;(lanes[c] ??= []).push(name)
  })
  const position = new Map<string, [number, number]>()
  lanes.forEach((lane, x) => lane?.forEach((name, y) => position.set(name, [x * (NODE_W + GAP_X), y * (NODE_H + GAP_Y)])))

  const width = lanes.length * (NODE_W + GAP_X) - GAP_X + 2
  const height = Math.max(...lanes.map((lane) => lane?.length ?? 0)) * (NODE_H + GAP_Y) - GAP_Y + 2

  const edges = names.flatMap((name) =>
    graph[name].dependencies.filter((d) => position.has(d)).map((dep) => ({ from: dep, to: name })),
  )

  return (
    <div className="figure scroll scroll-x">
      <div className="graph-wrap">
        <svg className="graph" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-labelledby="graph-title">
          <title id="graph-title">Service dependency graph</title>
          {edges.map(({ from, to }) => {
            const [x1, y1] = position.get(from)!
            const [x2, y2] = position.get(to)!
            const sx = x1 + NODE_W + 1
            const sy = y1 + NODE_H / 2 + 1
            const ex = x2 - ARROW
            const ey = y2 + NODE_H / 2 + 1
            const mx = (sx + ex) / 2
            return (
              <g key={`${from}→${to}`}>
                <path className="g-edge" d={`M${sx} ${sy} C${mx} ${sy} ${mx} ${ey} ${ex} ${ey}`} />
                <path className="g-arrow" d={`M${ex} ${ey - 4} L${ex + ARROW} ${ey} L${ex} ${ey + 4} Z`} />
              </g>
            )
          })}
          {names.map((name) => {
            const [x, y] = position.get(name)!
            return (
              <g className="g-node" key={name}>
                <title>{name}</title>
                <rect x={x + 0.5} y={y + 0.5} width={NODE_W} height={NODE_H} />
                <text x={x + 12} y={y + NODE_H / 2 + 5}>
                  {shortName(name)}
                </text>
              </g>
            )
          })}
        </svg>
      </div>
      <div className="graph-list">
        {names.map((name) => {
          const deps = graph[name].dependencies
          return (
            <div className="row" key={name}>
              <div className="r-toggle static">
                <span />
                <span className="r-text">
                  <span className="r-title">
                    <span className="r-name" title={name}>
                      {name}
                    </span>
                  </span>
                  {deps.length > 0 && (
                    <span className="r-meta">
                      <span>after {deps.join(', ')}</span>
                    </span>
                  )}
                </span>
              </div>
              <div className="r-actions" />
              <div className="r-metric" />
            </div>
          )
        })}
      </div>
    </div>
  )
}
