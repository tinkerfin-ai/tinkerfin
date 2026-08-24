import { BrandMark } from '../../../components/ui/BrandMark'
import { useI18n } from '../../../i18n'

export function EmptyConversationBrand() {
  return (
    <div className="empty-brand-lockup" aria-hidden="true">
      <BrandMark size={34} />
      <span className="empty-brand-name">TinkerFin</span>
      <span className="brand-plus">Plus</span>
    </div>
  )
}

export function EmptyConversation() {
  const { t } = useI18n()
  return (
    <div className="empty-conversation">
      <h2 className="visually-hidden">{t('暂无消息')}</h2>
      <p className="visually-hidden">{t('发送一条消息，开始新的真实对话流。')}</p>
    </div>
  )
}
