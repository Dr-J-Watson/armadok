// The map, drawn with Cytoscape (canvas): objects in columns by type, links as arrows. The text
// list next to it offers the same selection by keyboard, because a canvas cannot be read by
// assistive technology.

import cytoscape from 'cytoscape'
import type { Core, ElementDefinition } from 'cytoscape'
import { Expand, Minimize2, Minus, Plus, ScanSearch } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { KNOWN_TYPES, layoutMap } from './graph'

interface Props {
  elements: ElementDefinition[]
  selected: string | null
  onSelect: (key: string | null) => void
  // Changes when the theme changes, so colours are read again.
  themeKey: string
}

// Colours come from CSS variables so themes apply to the map too.
function cssColor(name: string, fallback: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
}

// Black or white text, whichever reads better on this background (#rgb or #rrggbb).
function textOn(background: string): string {
  const hex = background.replace('#', '')
  const full = hex.length === 3 ? [...hex].map((c) => c + c).join('') : hex
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16))
  return 0.299 * r + 0.587 * g + 0.114 * b > 150 ? '#14181f' : '#ffffff'
}

function stylesheet(): cytoscape.StylesheetJson {
  const text = cssColor('--text', '#222')
  const muted = cssColor('--muted', '#777')
  const edge = cssColor('--muted', '#8a93a3')
  const accent = cssColor('--accent', '#06f')
  const surface = cssColor('--surface', '#fff')
  const typeStyles = [...KNOWN_TYPES].map((type) => {
    const color = cssColor(`--t-${type}`, '#888')
    return { selector: `node[type = "${type}"]`, style: { 'background-color': color, color: textOn(color) } }
  })
  const other = cssColor('--t-other', '#888')
  return [
    {
      selector: 'node',
      style: {
        shape: 'round-rectangle',
        'background-color': other,
        color: textOn(other),
        label: 'data(label)',
        'font-size': 12,
        'font-weight': 600,
        'text-valign': 'center',
        'text-halign': 'center',
        'text-wrap': 'ellipsis',
        'text-max-width': '134px',
        width: 150,
        height: 30,
        'border-width': 0,
        'overlay-opacity': 0,
      },
    },
    ...typeStyles,
    {
      selector: 'node.colhead',
      style: {
        shape: 'rectangle',
        'background-opacity': 0,
        color: muted,
        'font-size': 12,
        'font-weight': 700,
        'text-transform': 'uppercase',
        'text-max-width': '190px',
        'text-wrap': 'none',
        events: 'no',
        width: 150,
        height: 20,
      },
    },
    {
      selector: 'edge',
      style: {
        width: 1.6,
        'line-color': edge,
        'target-arrow-color': edge,
        'target-arrow-shape': 'triangle',
        'arrow-scale': 0.9,
        'curve-style': 'bezier',
        opacity: 0.55,
        'overlay-opacity': 0,
        'font-size': 10,
        color: muted,
        'text-background-color': surface,
        'text-background-opacity': 1,
        'text-background-padding': '2px',
      },
    },
    { selector: '.dim', style: { opacity: 0.12 } },
    { selector: 'node.near', style: { 'border-width': 2, 'border-color': accent } },
    { selector: 'node:selected', style: { 'border-width': 4, 'border-color': accent } },
    {
      selector: 'edge.hot',
      style: { 'line-color': accent, 'target-arrow-color': accent, width: 3, opacity: 1, label: 'data(kindLabel)', 'text-rotation': 'autorotate', color: text },
    },
  ] as cytoscape.StylesheetJson
}

export function GraphView({ elements, selected, onSelect, themeKey }: Props) {
  const { t } = useTranslation('relations')
  const container = useRef<HTMLDivElement>(null)
  const cy = useRef<Core | null>(null)
  const [big, setBig] = useState(false)
  const onSelectRef = useRef(onSelect)
  useEffect(() => {
    onSelectRef.current = onSelect
  }, [onSelect])

  // The drawing's data: the server's objects placed in columns, plus a title above each column
  // and a readable name on each link.
  const drawn = useMemo(() => {
    const nodes = elements.filter((e) => e.group === 'nodes').map((e) => ({ id: String(e.data.id), type: String(e.data.type) }))
    const edges = elements.filter((e) => e.group === 'edges').map((e) => ({ source: String(e.data.source), target: String(e.data.target) }))
    const layout = layoutMap(nodes, edges)
    const heads: ElementDefinition[] = layout.columns.map((c, i) => ({
      group: 'nodes',
      classes: 'colhead',
      data: { id: `__head${i}`, label: c.types.map((type) => t(`types.${type}`, { defaultValue: type })).join(' · '), type: '__head' },
      position: { x: c.x, y: -50 },
      selectable: false,
      grabbable: false,
    }))
    const placed = elements.map((e) =>
      e.group === 'nodes'
        ? { ...e, position: layout.positions[String(e.data.id)] }
        : { ...e, data: { ...e.data, kindLabel: t(`kinds.${String(e.data.kind)}`, { defaultValue: String(e.data.kind) }) } },
    )
    return [...heads, ...placed]
  }, [elements, t])

  // The drawing is rebuilt only when what it shows really changes (not at every refresh or
  // render), and the zoom and position the person chose are put back afterwards.
  const signature = JSON.stringify(drawn)
  const drawnRef = useRef(drawn)
  useEffect(() => {
    drawnRef.current = drawn
  })
  const scope = 'all'
  const view = useRef<{ scope: string; zoom: number; pan: { x: number; y: number } } | null>(null)
  useEffect(() => {
    if (!container.current) return
    const keep = view.current && view.current.scope === scope ? view.current : null
    const instance = cytoscape({
      container: container.current,
      elements: drawnRef.current,
      style: stylesheet(),
      layout: { name: 'preset', fit: !keep, padding: 30 },
      wheelSensitivity: 0.25,
      minZoom: 0.15,
      maxZoom: 2.5,
      ...(keep ? { zoom: keep.zoom, pan: keep.pan } : {}),
    })
    instance.on('tap', 'node', (event) => onSelectRef.current(event.target.id()))
    instance.on('tap', (event) => {
      if (event.target === instance) onSelectRef.current(null)
    })
    // Hovering lights up what a node is connected to.
    instance.on('mouseover', 'node[type != "__head"]', (event) => {
      container.current?.style.setProperty('cursor', 'pointer')
      event.target.neighborhood('node').addClass('near')
    })
    instance.on('mouseout', 'node', () => {
      container.current?.style.removeProperty('cursor')
      instance.nodes().removeClass('near')
    })
    cy.current = instance
    return () => {
      view.current = { scope, zoom: instance.zoom(), pan: instance.pan() }
      instance.destroy()
      cy.current = null
    }
  }, [signature, themeKey, scope])

  // The canvas keeps its size by itself only if told when its box changes.
  useEffect(() => {
    cy.current?.resize()
    cy.current?.fit(undefined, 30)
  }, [big])

  // Show what is selected: its neighbourhood stays bright, the rest fades.
  useEffect(() => {
    const instance = cy.current
    if (!instance) return
    instance.batch(() => {
      instance.elements().removeClass('dim hot')
      instance.nodes().unselect()
      if (!selected) return
      const node = instance.getElementById(selected)
      if (node.empty()) return
      node.select()
      const around = node.closedNeighborhood()
      instance.elements().not(around).addClass('dim')
      node.connectedEdges().addClass('hot')
    })
    if (selected) {
      const node = instance.getElementById(selected)
      // Only move the view when the object is out of sight; never rescale it.
      if (!node.empty()) {
        const box = node.boundingBox()
        const seen = instance.extent()
        const inside = box.x1 >= seen.x1 && box.x2 <= seen.x2 && box.y1 >= seen.y1 && box.y2 <= seen.y2
        if (!inside) instance.animate({ center: { eles: node }, duration: 200 })
      }
    }
  }, [selected, signature, themeKey])

  const zoom = (factor: number) => {
    const instance = cy.current
    if (instance) instance.zoom({ level: instance.zoom() * factor, renderedPosition: { x: instance.width() / 2, y: instance.height() / 2 } })
  }

  return (
    <div className={`map-frame${big ? ' map-big' : ''}`}>
      <div ref={container} className="map-canvas" role="img" aria-label={t('canvasLabel')} />
      <div className="map-tools" role="toolbar" aria-label={t('tools')}>
        <button type="button" className="icon-button" title={t('zoomIn')} aria-label={t('zoomIn')} onClick={() => zoom(1.3)}>
          <Plus size={16} aria-hidden="true" />
        </button>
        <button type="button" className="icon-button" title={t('zoomOut')} aria-label={t('zoomOut')} onClick={() => zoom(1 / 1.3)}>
          <Minus size={16} aria-hidden="true" />
        </button>
        <button type="button" className="icon-button" title={t('fit')} aria-label={t('fit')} onClick={() => cy.current?.fit(undefined, 30)}>
          <ScanSearch size={16} aria-hidden="true" />
        </button>
        <button type="button" className="icon-button" title={t(big ? 'shrink' : 'enlarge')} aria-label={t(big ? 'shrink' : 'enlarge')} aria-pressed={big} onClick={() => setBig(!big)}>
          {big ? <Minimize2 size={16} aria-hidden="true" /> : <Expand size={16} aria-hidden="true" />}
        </button>
      </div>
      <p className="map-hint hint">{t('hint')}</p>
    </div>
  )
}
