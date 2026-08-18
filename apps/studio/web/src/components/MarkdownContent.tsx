import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { memo } from 'react'
import type { ReactNode } from 'react'

const bareAsciiUrl = /^(https?:\/\/[A-Za-z0-9.-]+(?::\d+)?(?:[/?#][A-Za-z0-9\-._~:/?#[\]@!$&'()*+,;=%]*)?)([\s\S]*)$/i

function splitBareUrl(children: ReactNode) {
  const text = typeof children === 'string'
    ? children
    : Array.isArray(children) && children.every((child) => typeof child === 'string')
      ? children.join('')
      : null
  if (text == null) return null
  const match = text.match(bareAsciiUrl)
  if (!match || !match[2]) return null

  const trailingPunctuation = match[1].match(/[.,!?;:]+$/)?.[0] ?? ''
  return {
    literal: text,
    url: trailingPunctuation ? match[1].slice(0, -trailingPunctuation.length) : match[1],
    suffix: `${trailingPunctuation}${match[2]}`,
  }
}

function isLiteralAutolink(href: string | undefined, literal: string) {
  if (!href?.startsWith('http')) return false
  try {
    return decodeURI(href) === literal
  } catch {
    return href === literal
  }
}

function externalLinkProps(href: string | undefined) {
  return href && /^https?:\/\//i.test(href)
    ? { target: '_blank', rel: 'noopener noreferrer' }
    : {}
}

function MarkdownContentView({
  content,
  className,
}: {
  content: string
  className?: string
}) {
  return (
    <div className={className ? `markdown-content ${className}` : 'markdown-content'}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a({ children, href }) {
            const bareUrl = splitBareUrl(children)
            if (bareUrl && isLiteralAutolink(href, bareUrl.literal)) {
              return <><a href={bareUrl.url} {...externalLinkProps(bareUrl.url)}>{bareUrl.url}</a>{bareUrl.suffix}</>
            }
            return <a href={href} {...externalLinkProps(href)}>{children}</a>
          },
          table({ children }) {
            return (
              <div className="markdown-table-wrap">
                <table>{children}</table>
              </div>
            )
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}

export const MarkdownContent = memo(MarkdownContentView)
