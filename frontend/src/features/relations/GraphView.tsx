// The bubble map, drawn with Cytoscape (canvas). The text list next to it offers the same
// selection by keyboard, because a canvas cannot be read by assistive technology.

import cytoscape from 'cytoscape'
import type { Core, ElementDefinition } from 'cytoscape'
import { useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { KNOWN_TYPES } from './graph'

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

function stylesheet(): cytoscape.StylesheetJson {
  const text = cssColor('--text', '#222')
  const border = cssColor('--border', '#999')
  const typeStyles = [...KNOWN_TYPES].map((type) => ({
    selector: `node[type = "${type}"]`,
    style: { 'background-color': cssColor(`--t-${type}`, '#888') },
  }))
  return [
    {
      selector: 'node',
      style: {
        'background-color': cssColor('--t-other', '#888'),
        label: 'data(label)',
        color: text,
        'font-size': 11,
        'text-valign': 'bottom',
        'text-margin-y': 4,
        'text-wrap': 'ellipsis',
        'text-max-width': '110px',
        width: 26,
        height: 26,
        'border-width': 1,
        'border-color': border,
      },
    },
    ...typeStyles,
    {
      selector: 'edge',
      style: { width: 1.5, 'line-color': border, 'curve-style': 'bezier', opacity: 0.8 },
    },
    { selector: '.dim', style: { opacity: 0.15 } },
    {
      selector: 'node:selected',
      style: { 'border-width': 4, 'border-color': cssColor('--accent', '#06f') },
    },
    {
      selector: 'edge.hot',
      style: { 'line-color': cssColor('--accent', '#06f'), width: 3, opacity: 1 },
    },
  ] as cytoscape.StylesheetJson
}

export function GraphView({ elements, selected, onSelect, themeKey }: Props) {
  const { t } = useTranslation('relations')
  const container = useRef<HTMLDivElement>(null)
  const cy = useRef<Core | null>(null)
  const onSelectRef = useRef(onSelect)
  useEffect(() => {
    onSelectRef.current = onSelect
  }, [onSelect])

  // Build the drawing again when the data or the theme changes.
  useEffect(() => {
    if (!container.current) return
    const instance = cytoscape({
      container: container.current,
      elements,
      style: stylesheet(),
      layout: { name: 'cose', animate: false, nodeRepulsion: () => 9000, idealEdgeLength: () => 70 },
      wheelSensitivity: 0.3,
    })
    instance.on('tap', 'node', (event) => onSelectRef.current(event.target.id()))
    instance.on('tap', (event) => {
      if (event.target === instance) onSelectRef.current(null)
    })
    cy.current = instance
    return () => {
      instance.destroy()
      cy.current = null
    }
  }, [elements, themeKey])

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
      if (!node.empty()) instance.animate({ center: { eles: node }, duration: 200 })
    }
  }, [selected, elements, themeKey])

  return <div ref={container} className="map-canvas" role="img" aria-label={t('canvasLabel')} />
}
