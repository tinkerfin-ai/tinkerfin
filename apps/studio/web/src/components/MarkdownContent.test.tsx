import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { MarkdownContent } from './MarkdownContent'

describe('MarkdownContent links', () => {
  it('keeps Chinese instructions after a bare URL outside the link', () => {
    const content = '访问 https://www.baidu.com，了解该网站的主营业务。返回不超过50字的中文总结，说明百度是做什么业务的。'
    render(<MarkdownContent content={content} />)

    const link = screen.getByRole('link')
    expect(link).toHaveAttribute('href', 'https://www.baidu.com')
    expect(link).toHaveTextContent('https://www.baidu.com')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(link).not.toHaveTextContent('了解该网站的主营业务')
    expect(link.parentElement).toHaveTextContent(content)
  })

  it('preserves explicit Markdown link labels', () => {
    render(<MarkdownContent content="访问 [百度官网](https://www.baidu.com) 和 [https://www.baidu.com中文说明](https://example.com)" />)

    const link = screen.getByRole('link', { name: '百度官网' })
    expect(link).toHaveAttribute('href', 'https://www.baidu.com')
    expect(screen.getByRole('link', { name: 'https://www.baidu.com中文说明' })).toHaveAttribute(
      'href',
      'https://example.com',
    )
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('keeps relative application links in the current tab', () => {
    render(<MarkdownContent content="[会话帮助](/help/conversations)" />)

    const link = screen.getByRole('link', { name: '会话帮助' })
    expect(link).not.toHaveAttribute('target')
    expect(link).not.toHaveAttribute('rel')
  })
})
