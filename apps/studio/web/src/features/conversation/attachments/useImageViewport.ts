import { useEffect, useRef, useState } from 'react'
import type { PointerEvent } from 'react'

/** 以完整图片为初始视图，放大后将拖动限制在可查看的图片范围内 */
export function useImageViewport(imageId: string) {
  const stage = useRef<HTMLDivElement>(null)
  const [bounds, setBounds] = useState({ width: 0, height: 0 })
  const [image, setImage] = useState({ width: 0, height: 0 })
  const [zoom, setZoom] = useState<number | 'fit'>('fit')
  const [offset, setOffset] = useState({ x: 0, y: 0 })
  const drag = useRef<{
    id: number
    x: number
    y: number
    offsetX: number
    offsetY: number
  } | null>(null)
  useEffect(() => {
    setImage({ width: 0, height: 0 })
    setZoom('fit')
    setOffset({ x: 0, y: 0 })
    drag.current = null
  }, [imageId])
  useEffect(() => {
    const element = stage.current
    if (!element) return
    const update = () =>
      setBounds({ width: element.clientWidth, height: element.clientHeight })
    update()
    const observer = new ResizeObserver(update)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  const fit =
    image.width && image.height && bounds.width && bounds.height
      ? Math.min(bounds.width / image.width, bounds.height / image.height)
      : 1
  const scale = zoom === 'fit' ? fit : zoom
  const limitX = Math.max(0, (image.width * scale - bounds.width) / 2)
  const limitY = Math.max(0, (image.height * scale - bounds.height) / 2)
  const x = Math.max(-limitX, Math.min(limitX, offset.x))
  const y = Math.max(-limitY, Math.min(limitY, offset.y))
  const changeZoom = (value: number | 'fit') => {
    setZoom(value)
    setOffset({ x: 0, y: 0 })
    drag.current = null
  }
  const pointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || (!limitX && !limitY)) return
    event.currentTarget.setPointerCapture(event.pointerId)
    drag.current = {
      id: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      offsetX: x,
      offsetY: y,
    }
  }
  const pointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const start = drag.current
    if (start?.id !== event.pointerId) return
    setOffset({
      x: Math.max(
        -limitX,
        Math.min(limitX, start.offsetX + event.clientX - start.x),
      ),
      y: Math.max(
        -limitY,
        Math.min(limitY, start.offsetY + event.clientY - start.y),
      ),
    })
  }
  return {
    stage,
    image,
    setImage,
    scale,
    zoom,
    changeZoom,
    zoomIn: () => changeZoom(Math.min(Math.max(4, fit), scale * 1.25)),
    zoomOut: () => changeZoom(Math.max(Math.min(fit, 0.1), scale / 1.25)),
    canZoomIn: scale < Math.max(4, fit),
    canZoomOut: scale > Math.min(fit, 0.1),
    imageStyle: {
      width: image.width ? image.width * scale : undefined,
      height: image.height ? image.height * scale : undefined,
      transform: `translate(${x}px, ${y}px)`,
    },
    canPan: Boolean(limitX || limitY),
    pointerDown,
    pointerMove,
    pointerEnd: () => {
      drag.current = null
    },
  }
}
