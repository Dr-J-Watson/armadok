// QR code drawn as SVG from the module matrix (no image, no HTML injection). Always dark on
// light, whatever the theme: scanners need the contrast.

import QRCode from 'qrcode'
import { useMemo } from 'react'

export function QrCode({ value, label }: { value: string; label: string }) {
  const { size, path } = useMemo(() => {
    const { modules } = QRCode.create(value, { errorCorrectionLevel: 'M' })
    let d = ''
    for (let y = 0; y < modules.size; y++)
      for (let x = 0; x < modules.size; x++)
        if (modules.get(x, y)) d += `M${x + 4} ${y + 4}h1v1h-1z`
    return { size: modules.size + 8, path: d } // 4 modules of quiet zone on every side
  }, [value])

  return (
    <svg
      className="qr"
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      aria-label={label}
      shapeRendering="crispEdges"
    >
      <rect width={size} height={size} fill="#fff" />
      <path d={path} fill="#000" />
    </svg>
  )
}
