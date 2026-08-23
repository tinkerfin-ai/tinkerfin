import { BrandMark } from '../../../components/ui/BrandMark'

export function EmptyConversation() {
  return (
    <div className="empty-conversation">
      <div className="empty-conversation-inner">
        <div className="empty-mark"><BrandMark /></div>
        <h2>暂无消息</h2>
        <p className="empty-lead">发送一条消息，开始新的真实对话流。</p>
      </div>
    </div>
  )
}
