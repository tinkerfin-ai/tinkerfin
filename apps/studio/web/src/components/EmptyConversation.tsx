import { BrandMark } from './BrandMark'
import { TypographyReveal } from './TypographyReveal'

export function EmptyConversation() {
  return (
    <div className="empty-conversation">
      <div className="empty-conversation-inner">
        <div className="empty-mark"><BrandMark /></div>
        <p className="eyebrow">TinkerFin Agent</p>
        <TypographyReveal as="h2" variant="display">暂无消息</TypographyReveal>
        <p className="empty-lead">发送一条消息，开始新的真实对话流。</p>
      </div>
    </div>
  )
}
