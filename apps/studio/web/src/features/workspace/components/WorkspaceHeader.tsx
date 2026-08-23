import { Check, ChevronDown, GitBranch, Menu, PanelRight } from 'lucide-react'
import type { RefObject } from 'react'

import type { AgentMode } from '../../../api/conversation/types'
import { Button, IconButton } from '../../../components/ui'
import { ThemePicker } from '../../../components/ui/ThemePicker'
import { ListboxPicker } from './ListboxPicker'
import { OverflowMarquee } from './OverflowMarquee'

const AGENT_MODES = ['default', 'plan'] as const satisfies readonly AgentMode[]

function ModelOption({ label, selected }: { label: string; selected: boolean }) {
  return (
    <>
      <OverflowMarquee className="model-option-label">{label}</OverflowMarquee>
      <span className="model-option-check" aria-hidden="true">{selected && <Check size={14} />}</span>
    </>
  )
}

export function WorkspaceHeader({
  conversationTitle,
  model,
  modelIds,
  defaultModelId,
  modelDisplayName,
  modelCatalogStatus,
  isModelPickerOpen,
  onModelPickerOpenChange,
  onSelectModel,
  mode,
  isAgentPresetPickerOpen,
  onAgentPresetPickerOpenChange,
  onSelectAgentMode,
  agentPresetDisabled,
  drawerOpen,
  todoCount,
  drawerToggleRef,
  overlayTriggerRef,
  onOpenOverlay,
  onToggleDrawer,
  onRetryModels,
}: {
  conversationTitle: string
  model: string
  modelIds: string[]
  defaultModelId: string
  modelDisplayName: (modelId: string) => string
  modelCatalogStatus: 'loading' | 'ready' | 'error'
  isModelPickerOpen: boolean
  onModelPickerOpenChange: (open: boolean) => void
  onSelectModel: (model: string) => void
  mode: AgentMode
  isAgentPresetPickerOpen: boolean
  onAgentPresetPickerOpenChange: (open: boolean) => void
  onSelectAgentMode: (mode: AgentMode) => void
  agentPresetDisabled: boolean
  drawerOpen: boolean
  todoCount: number
  drawerToggleRef: RefObject<HTMLButtonElement | null>
  overlayTriggerRef: RefObject<HTMLButtonElement | null>
  onOpenOverlay: () => void
  onToggleDrawer: () => void
  onRetryModels: () => void
}) {
  const selectedModel = modelIds.includes(model) ? model : defaultModelId

  return (
    <header className="chat-header">
      <div className="header-left">
        <IconButton ref={overlayTriggerRef} className="menu-toggle" label="打开导航" icon={<Menu size={19} />} onClick={onOpenOverlay} />
        <h1 className="visually-hidden">{conversationTitle || '新会话'}</h1>
        <ListboxPicker
          value={selectedModel}
          options={modelIds}
          open={isModelPickerOpen}
          onOpenChange={onModelPickerOpenChange}
          onChange={onSelectModel}
          disabled={modelCatalogStatus !== 'ready' || modelIds.length === 0}
          triggerLabel="选择模型"
          listboxLabel="模型选项"
          rootClassName="model-picker"
          triggerClassName="model-select"
          listboxClassName="model-options"
          optionClassName="overflow-marquee-trigger"
          renderTrigger={(selected) => <><span>{modelDisplayName(selected) || '加载模型…'}</span><ChevronDown size={14} /></>}
          renderOption={(option, selected) => <ModelOption label={modelDisplayName(option)} selected={selected} />}
        />
        {modelCatalogStatus === 'error' && (
          <div className="model-catalog-error" role="alert">
            <span>模型加载失败</span>
            <Button size="sm" onClick={onRetryModels}>重试</Button>
          </div>
        )}
      </div>
      <div className="header-actions">
        <ThemePicker />
        <ListboxPicker
          value={mode}
          options={AGENT_MODES}
          open={isAgentPresetPickerOpen}
          onOpenChange={onAgentPresetPickerOpenChange}
          onChange={onSelectAgentMode}
          disabled={agentPresetDisabled}
          triggerLabel="当前 Agent 预设"
          listboxLabel="Agent 预设选项"
          rootClassName="agent-preset-picker"
          triggerClassName="agent-preset"
          listboxClassName="agent-preset-options"
          renderTrigger={(selected) => <><GitBranch size={16} /><span className="agent-preset-label">{selected}</span><ChevronDown size={14} /></>}
          renderOption={(option, selected) => <><span>{option}</span>{selected && <Check size={14} />}</>}
        />
        <Button
          ref={drawerToggleRef}
          className="drawer-toggle"
          variant="secondary"
          selected={drawerOpen}
          leadingIcon={<PanelRight size={17} />}
          aria-label={drawerOpen ? '关闭任务抽屉' : '打开任务抽屉'}
          aria-expanded={drawerOpen}
          aria-controls="task-drawer"
          onClick={onToggleDrawer}
        >
          任务 <b>{todoCount}</b>
        </Button>
      </div>
    </header>
  )
}
