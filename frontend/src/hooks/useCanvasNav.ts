// canvas navigation hook — pan + zoom for the war room agent table.
//
// pan:  drag on the canvas background (clicks on seats/orbs are ignored)
// zoom: scroll wheel, centered on the cursor position so the canvas zooms "into" the mouse
// scale is clamped between MIN_SCALE and MAX_SCALE

import { useRef, useState, useCallback, useEffect } from 'react'

export interface Transform { x: number; y: number; scale: number }

const MIN_SCALE = 0.25
const MAX_SCALE = 2.5

export function useCanvasNav(initialScale = 1) {
  const [transform, setTransform] = useState<Transform>({ x: 0, y: 0, scale: initialScale })
  const dragging    = useRef(false)
  const lastPos     = useRef({ x: 0, y: 0 })
  const containerRef = useRef<HTMLDivElement>(null)

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    // ignore clicks on interactive elements — only pan on the canvas background
    if ((e.target as HTMLElement).closest('.wr-seat, .wr-spawned-bubble, .wr-drawer')) return
    dragging.current = true
    lastPos.current = { x: e.clientX, y: e.clientY }
    // pointer capture keeps the drag alive even if the pointer leaves the container
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  }, [])

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    if (!dragging.current) return
    const dx = e.clientX - lastPos.current.x
    const dy = e.clientY - lastPos.current.y
    lastPos.current = { x: e.clientX, y: e.clientY }
    setTransform(t => ({ ...t, x: t.x + dx, y: t.y + dy }))
  }, [])

  const onPointerUp = useCallback(() => {
    dragging.current = false
  }, [])

  const onWheel = useCallback((e: WheelEvent) => {
    e.preventDefault()
    const container = containerRef.current
    if (!container) return
    const rect = container.getBoundingClientRect()

    // cursor position relative to the container (not the viewport)
    const cx = e.clientX - rect.left
    const cy = e.clientY - rect.top

    setTransform(t => {
      const delta    = e.deltaY > 0 ? 0.9 : 1.1
      const newScale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, t.scale * delta))
      const ratio    = newScale / t.scale
      // shift the origin so the point under the cursor stays fixed during zoom
      return {
        scale: newScale,
        x: cx - ratio * (cx - t.x),
        y: cy - ratio * (cy - t.y),
      }
    })
  }, [])

  // wheel must be attached as a non-passive listener so we can call preventDefault()
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [onWheel])

  const resetView = useCallback(() => {
    setTransform({ x: 0, y: 0, scale: 1 })
  }, [])

  return { transform, containerRef, onPointerDown, onPointerMove, onPointerUp, resetView }
}
