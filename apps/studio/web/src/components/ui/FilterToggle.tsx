import { Check } from 'lucide-react'

export interface FilterToggleProps {
  pressed: boolean
  label: string
  onPressedChange: (pressed: boolean) => void
}

/** 使用与筛选器一致的中性视觉表达布尔筛选条件 */
export function FilterToggle({
  pressed,
  label,
  onPressedChange,
}: FilterToggleProps) {
  return (
    <button
      type="button"
      className="ui-filter-control ui-filter-toggle"
      aria-pressed={pressed}
      onClick={() => onPressedChange(!pressed)}
    >
      <span className="ui-filter-toggle__mark" aria-hidden="true">
        {pressed && <Check size={12} />}
      </span>
      <span>{label}</span>
    </button>
  )
}
