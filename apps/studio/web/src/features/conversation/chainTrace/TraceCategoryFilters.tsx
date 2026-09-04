import {
  FolderOpen,
  Link2,
  MessageCircleMore,
  WandSparkles,
} from 'lucide-react'

import { Button } from '../../../components/ui'
import { useI18n } from '../../../i18n'
import {
  TRACE_PUBLIC_CATEGORIES,
  traceVisualCategoryLabel,
  type TracePublicCategory,
} from './tracePresentation'

const categoryIcon = (category: TracePublicCategory) => ({
  user: <MessageCircleMore size={14} />,
  assistant: <WandSparkles size={14} />,
  tool: <Link2 size={14} />,
  context: <FolderOpen size={14} />,
}[category])

export function TraceCategoryFilters({
  selected,
  onToggle,
}: {
  selected: ReadonlySet<TracePublicCategory>
  onToggle: (category: TracePublicCategory) => void
}) {
  const { t } = useI18n()
  return (
    <div className="chain-trace-category-filters" role="group" aria-label={t('节点类型')}>
      {TRACE_PUBLIC_CATEGORIES.map((category) => {
        const pressed = selected.has(category)
        return (
          <Button
            key={category}
            size="xs"
            variant="ghost"
            selected={pressed}
            disabled={pressed && selected.size === 1}
            className={`chain-trace-category-filter is-${category}`}
            leadingIcon={categoryIcon(category)}
            onClick={() => onToggle(category)}
          >
            {traceVisualCategoryLabel(category, t)}
          </Button>
        )
      })}
    </div>
  )
}
