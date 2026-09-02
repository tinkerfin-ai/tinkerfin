import { Check, ChevronDown } from 'lucide-react'
import {
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
} from 'react'

import { ListboxPicker } from './ListboxPicker'

const FILTER_OVERLAY_MIN_WIDTH = 176
const FILTER_OVERLAY_VIEWPORT_INSET = 8
const FILTER_OVERLAY_GAP = 6

export interface FilterPickerProps<T extends string> {
  value: T
  options: readonly T[]
  label: string
  renderLabel: (value: T) => string
  onChange: (value: T) => void
}

/** 统一紧凑筛选器的视觉、弹层位置、键盘操作与焦点恢复 */
export function FilterPicker<T extends string>({
  value,
  options,
  label,
  renderLabel,
  onChange,
}: FilterPickerProps<T>) {
  const rootRef = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false)
  const [listboxStyle, setListboxStyle] = useState<CSSProperties>()

  const positionListbox = useCallback(() => {
    const trigger = rootRef.current?.querySelector('button')
    if (!trigger) return
    const triggerRect = trigger.getBoundingClientRect()
    const width = Math.max(triggerRect.width, FILTER_OVERLAY_MIN_WIDTH)
    setListboxStyle({
      position: 'fixed',
      top: triggerRect.bottom + FILTER_OVERLAY_GAP,
      left: Math.max(
        FILTER_OVERLAY_VIEWPORT_INSET,
        Math.min(
          triggerRect.left,
          window.innerWidth - width - FILTER_OVERLAY_VIEWPORT_INSET,
        ),
      ),
      minWidth: width,
    })
  }, [])

  useLayoutEffect(() => {
    if (!open) return undefined
    positionListbox()
    window.addEventListener('resize', positionListbox)
    window.addEventListener('scroll', positionListbox, true)
    return () => {
      window.removeEventListener('resize', positionListbox)
      window.removeEventListener('scroll', positionListbox, true)
    }
  }, [open, positionListbox])

  return (
    <div ref={rootRef} className="ui-filter-picker-anchor">
      <ListboxPicker
        value={value}
        options={options}
        open={open}
        onOpenChange={(nextOpen) => {
          if (nextOpen) positionListbox()
          setOpen(nextOpen)
        }}
        onChange={onChange}
        triggerLabel={`${label}：${renderLabel(value)}`}
        listboxLabel={label}
        rootClassName="ui-filter-picker"
        triggerClassName="ui-filter-control ui-filter-picker__trigger"
        listboxClassName="ui-filter-picker__listbox"
        optionClassName="ui-filter-picker__option"
        listboxPortalTarget={typeof document === 'undefined' ? null : document.body}
        listboxStyle={listboxStyle}
        renderTrigger={(option) => (
          <>
            <span>{renderLabel(option)}</span>
            <ChevronDown size={13} aria-hidden="true" />
          </>
        )}
        renderOption={(option, selected) => (
          <>
            <span>{renderLabel(option)}</span>
            {selected && <Check size={14} aria-hidden="true" />}
          </>
        )}
      />
    </div>
  )
}
