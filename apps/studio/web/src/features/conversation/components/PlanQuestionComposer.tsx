import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  CircleHelp,
  MessageSquareText,
  X,
} from 'lucide-react'
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react'

import { Button, IconButton, OverlayScrollbar } from '../../../components/ui'
import type { PlanQuestionItem, PlanQuestionState } from '../../../types'
import { useI18n } from '../../../i18n'
import { ActivityDots } from './ActivityDots'
import {
  readPlanQuestionCollapsed,
  writePlanQuestionCollapsed,
} from '../planQuestionCollapse'

const questionAnswered = (question: PlanQuestionItem) => Boolean(
  question.selectedOptionId || question.customAnswer?.trim(),
)

const resizeCustomAnswer = (textarea: HTMLTextAreaElement | null) => {
  if (!textarea) return
  textarea.style.height = 'auto'
  const maxHeight = Number.parseFloat(window.getComputedStyle(textarea).maxHeight)
  const contentHeight = textarea.scrollHeight
  if (contentHeight <= 0) return
  const nextHeight = Number.isFinite(maxHeight)
    ? Math.min(contentHeight, maxHeight)
    : contentHeight
  textarea.style.height = `${nextHeight}px`
  textarea.style.overflowY = contentHeight > nextHeight ? 'auto' : 'hidden'
}

export function PlanQuestionStatusRow({ interaction }: { interaction: PlanQuestionState }) {
  const { t } = useI18n()
  return (
    <div className="plan-question-wait-state">
      <div className="plan-question-status-row" role={interaction.submitted ? 'status' : undefined}>
        <CircleHelp size={14} aria-hidden="true" />
        <strong>{t('提问')}</strong>
        <span className="plan-question-status-separator" aria-hidden="true" />
        <span>{interaction.submitted ? t('正在继续规划') : t('等待回答')}</span>
      </div>
      {!interaction.submitted && <ActivityDots label={t('等待回答')} />}
    </div>
  )
}

export function PlanQuestionComposer({
  threadId,
  interaction,
  onChange,
  onSubmit,
  onAbandon,
}: {
  threadId: string
  interaction: PlanQuestionState
  onChange: (updater: (current: PlanQuestionState) => PlanQuestionState) => void
  onSubmit: () => void
  onAbandon: () => void
}) {
  const { t } = useI18n()
  const [minimized, setMinimized] = useState(() => readPlanQuestionCollapsed(threadId))
  const [focusedAnswerIndex, setFocusedAnswerIndex] = useState(0)
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([])
  const customAnswerRef = useRef<HTMLTextAreaElement | null>(null)
  const bodyRef = useRef<HTMLDivElement | null>(null)
  const activeIndex = Math.min(
    interaction.activeQuestionIndex,
    interaction.questions.length - 1,
  )
  const question = interaction.questions[activeIndex]
  useEffect(() => {
    setMinimized(readPlanQuestionCollapsed(threadId))
  }, [threadId])

  useEffect(() => {
    setFocusedAnswerIndex(0)
  }, [activeIndex])

  useEffect(() => {
    if (minimized) return
    // 选项为空时，自由文本就是该问题唯一可回答入口
    const frame = window.requestAnimationFrame(() => (
      optionRefs.current[0] ?? customAnswerRef.current
    )?.focus())
    return () => window.cancelAnimationFrame(frame)
  }, [activeIndex, minimized])

  useEffect(() => {
    if (minimized) return
    resizeCustomAnswer(customAnswerRef.current)
  }, [activeIndex, minimized, question?.customAnswer])

  const progress = useMemo(() => interaction.questions.map((item, index) => ({
    index,
    state: index === activeIndex
      ? 'current'
      : item.skipped
        ? 'skipped'
        : questionAnswered(item)
          ? 'answered'
          : 'pending',
  })), [activeIndex, interaction.questions])

  if (!question) return null

  const setActiveQuestion = (index: number) => {
    onChange((current) => ({
      ...current,
      activeQuestionIndex: Math.max(0, Math.min(index, current.questions.length - 1)),
      error: undefined,
    }))
  }

  const updateQuestion = (
    update: (current: PlanQuestionItem) => PlanQuestionItem,
    nextIndex?: number,
  ) => {
    onChange((current) => ({
      ...current,
      activeQuestionIndex: nextIndex ?? current.activeQuestionIndex,
      error: undefined,
      questions: current.questions.map((item, index) => (
        index === activeIndex ? update(item) : item
      )),
    }))
  }

  const selectOption = (optionId: string) => {
    const nextIndex = activeIndex < interaction.questions.length - 1
      ? activeIndex + 1
      : activeIndex
    updateQuestion((current) => ({
      ...current,
      selectedOptionId: optionId,
      customAnswer: '',
      skipped: false,
    }), nextIndex)
  }

  const moveAnswerFocus = (direction: 1 | -1) => {
    if (question.options.length === 0) return
    const next = (
      focusedAnswerIndex
      + direction
      + question.options.length
    ) % question.options.length
    setFocusedAnswerIndex(next)
    optionRefs.current[next]?.focus()
  }

  const handleAnswerKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    optionId: string,
  ) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      moveAnswerFocus(event.key === 'ArrowDown' ? 1 : -1)
      return
    }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      selectOption(optionId)
    }
  }

  const continueFromCustom = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'ArrowUp' && event.currentTarget.value === '') {
      const lastOption = optionRefs.current.at(-1)
      if (lastOption) {
        event.preventDefault()
        lastOption.focus()
      }
      return
    }
    if (
      event.key !== 'Enter'
      || event.shiftKey
      || event.nativeEvent.isComposing
      || event.nativeEvent.keyCode === 229
    ) return
    event.preventDefault()
    if (!event.currentTarget.value.trim()) return
    if (activeIndex < interaction.questions.length - 1) setActiveQuestion(activeIndex + 1)
  }

  const skipQuestion = () => {
    if (question.required) return
    const nextIndex = activeIndex < interaction.questions.length - 1
      ? activeIndex + 1
      : activeIndex
    updateQuestion((current) => ({
      ...current,
      selectedOptionId: undefined,
      customAnswer: '',
      skipped: true,
    }), nextIndex)
  }

  const submit = () => {
    const firstMissing = interaction.questions.findIndex((item) => (
      item.required && !questionAnswered(item)
    ))
    if (firstMissing >= 0) {
      onChange((current) => ({
        ...current,
        activeQuestionIndex: firstMissing,
        error: t('请回答所有必填的 Plan 澄清问题'),
      }))
      return
    }
    onSubmit()
  }

  const toggleMinimized = () => {
    setMinimized((current) => {
      const next = !current
      writePlanQuestionCollapsed(threadId, next)
      return next
    })
  }

  return (
    <section
      className={`plan-question-composer${minimized ? ' is-minimized' : ''}`}
      aria-label={t('Plan 澄清问题')}
      onWheel={(event) => {
        const body = bodyRef.current
        if (!body || body.scrollHeight <= body.clientHeight) {
          event.preventDefault()
          event.stopPropagation()
          return
        }
        if (!body.contains(event.target as Node)) {
          event.preventDefault()
          event.stopPropagation()
          body.scrollTop += event.deltaY
        }
      }}
    >
      <header className="plan-question-composer-head">
        <button
          type="button"
          className="plan-question-toggle-surface"
          aria-label={minimized
            ? t('点击标题区域展开问题卡片')
            : t('点击标题区域收起问题卡片')}
          onClick={toggleMinimized}
        />
        <div className="plan-question-composer-heading">
          <h2>
            <CircleHelp size={16} aria-hidden="true" />
            <span>{interaction.title}</span>
            {minimized && <small>· {t('第 {current} / {total} 题', {
              current: activeIndex + 1,
              total: interaction.questions.length,
            })}</small>}
          </h2>
          <p>{minimized ? question.prompt : interaction.description}</p>
        </div>
        <div className="plan-question-composer-head-actions">
          <IconButton
            size="sm"
            className="plan-question-composer-head-button"
            label={minimized ? t('展开问题卡片') : t('收起问题卡片')}
            tooltip={minimized ? t('展开问题卡片') : t('收起问题卡片')}
            icon={minimized ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            aria-expanded={!minimized}
            onClick={toggleMinimized}
          />
          <IconButton
            size="sm"
            className="plan-question-composer-head-button"
            label={t('放弃本次 Plan 澄清')}
            tooltip={t('放弃本次 Plan 澄清')}
            icon={<X size={15} />}
            onClick={onAbandon}
          />
        </div>
      </header>

      {minimized && (
        <nav className="plan-question-progress" aria-label={t('问题进度')}>
          {progress.map((item) => (
            <button
              key={interaction.questions[item.index]?.id}
              type="button"
              className={`plan-question-progress-step is-${item.state}`}
              aria-label={t('查看第 {current} 题：{question}', {
                current: item.index + 1,
                question: interaction.questions[item.index]?.prompt ?? '',
              })}
              aria-current={item.state === 'current' ? 'step' : undefined}
              onClick={() => setActiveQuestion(item.index)}
            >
              <span aria-hidden="true" />
            </button>
          ))}
        </nav>
      )}

      {!minimized && (
        <>
          <div
            ref={bodyRef}
            className="plan-question-composer-body ui-scrollbar"
            role="region"
            aria-label={question.prompt}
          >
            <h3>
              <span>{question.prompt}</span>
              {!question.required && <small>{t('可选')}</small>}
            </h3>
            <div className="plan-question-options">
              {question.options.length > 0 && (
                <div className="plan-question-choice-list" role="radiogroup" aria-label={question.prompt}>
                  {question.options.map((option, optionIndex) => (
                <button
                  key={option.id}
                  ref={(node) => { optionRefs.current[optionIndex] = node }}
                  type="button"
                  role="radio"
                  aria-checked={question.selectedOptionId === option.id}
                  tabIndex={focusedAnswerIndex === optionIndex ? 0 : -1}
                  className={`plan-question-option${question.selectedOptionId === option.id ? ' is-selected' : ''}`}
                  onFocus={() => setFocusedAnswerIndex(optionIndex)}
                  onClick={() => selectOption(option.id)}
                  onKeyDown={(event) => handleAnswerKeyDown(event, option.id)}
                >
                  <span className="plan-question-option-index" aria-hidden="true">
                    {optionIndex + 1}
                  </span>
                  <span className="plan-question-option-copy">
                    <strong>{option.label}</strong>
                    {option.description && <small>{option.description}</small>}
                    {option.recommended && (
                      <span className="plan-question-option-recommended">{t('推荐')}</span>
                    )}
                  </span>
                </button>
                  ))}
                </div>
              )}
              {question.allowFreeText && (
                <label className={`plan-question-custom${question.customAnswer ? ' is-active' : ''}`}>
                  <span className="visually-hidden">{t('自定义回答：{question}', { question: question.prompt })}</span>
                  <span className="plan-question-option-index" aria-hidden="true">
                    <MessageSquareText size={13} />
                  </span>
                  <textarea
                    ref={customAnswerRef}
                    id={`plan-question-custom-${interaction.interruptId}-${question.id}`}
                    rows={1}
                    value={question.customAnswer ?? ''}
                    placeholder={t('输入你的答案')}
                    onFocus={() => setFocusedAnswerIndex(question.options.length)}
                    onChange={(event) => {
                      resizeCustomAnswer(event.currentTarget)
                      const value = event.currentTarget.value
                      updateQuestion((current) => ({
                        ...current,
                        selectedOptionId: undefined,
                        customAnswer: value,
                        skipped: false,
                      }))
                    }}
                    onKeyDown={continueFromCustom}
                  />
                </label>
              )}
            </div>
          </div>

          <OverlayScrollbar viewportRef={bodyRef} />
          <footer className="plan-question-composer-footer">
            <div className="plan-question-composer-pager">
              <IconButton
                size="sm"
                className="plan-question-pager-button"
                label={t('浏览上一题')}
                icon={<ChevronLeft size={16} />}
                disabled={activeIndex === 0}
                onClick={() => setActiveQuestion(activeIndex - 1)}
              />
              <span>{activeIndex + 1} / {interaction.questions.length}</span>
              <IconButton
                size="sm"
                className="plan-question-pager-button"
                label={t('浏览下一题')}
                icon={<ChevronRight size={16} />}
                disabled={activeIndex === interaction.questions.length - 1}
                onClick={() => setActiveQuestion(activeIndex + 1)}
              />
            </div>
            <p className="plan-question-composer-feedback" role="status">
              {interaction.error ?? ''}
            </p>
            <div className="plan-question-composer-actions">
              {!question.required && (
                <Button size="sm" variant="secondary" onClick={skipQuestion}>
                  {t('跳过本题')}
                </Button>
              )}
              {activeIndex < interaction.questions.length - 1 ? (
                <Button
                  size="sm"
                  variant="primary"
                  disabled={question.required && !questionAnswered(question)}
                  onClick={() => setActiveQuestion(activeIndex + 1)}
                >
                  {t('下一题')}
                </Button>
              ) : (
                <Button
                  size="sm"
                  variant="primary"
                  onClick={submit}
                >
                  {t('提交')}
                </Button>
              )}
            </div>
          </footer>
        </>
      )}
    </section>
  )
}
