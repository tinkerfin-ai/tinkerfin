import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

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

const completeFixture = `# 一级标题

第一段正文。

## 二级标题

第二段正文，用于核对连续段落节奏。

### 三级标题

#### 四级标题

1. 第一项
   - 二级项目
     1. 三级项目

> 一段引用

行内代码 \`const answer = 42\`。

长链接 [TinkerFin 前端规范文档](https://example.com/docs/frontend/visual-system/markdown-contract-and-accessibility-checklist)。

---

| 名称 | 说明 |
| --- | --- |
| TinkerFin | 智能体工作台 |

\`\`\`ts
const value = 42
\`\`\`
`

describe('MarkdownContent article contract', () => {
  it('preserves headings, three-level lists, blockquotes, separators and table semantics', () => {
    const { container } = render(<MarkdownContent content={completeFixture} />)

    expect(screen.getByRole('heading', { level: 1, name: '一级标题' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: '二级标题' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: '三级标题' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 4, name: '四级标题' })).toBeInTheDocument()
    const paragraphs = container.querySelectorAll('.markdown-content > p')
    expect(paragraphs).toHaveLength(4)
    expect(paragraphs[0]).toHaveTextContent('第一段正文')
    expect(paragraphs[1]).toHaveTextContent('第二段正文')
    expect(container.querySelectorAll('ol ol, ol ul, ul ol, ul ul').length).toBeGreaterThanOrEqual(2)
    expect(container.querySelector('blockquote')).toHaveTextContent('一段引用')
    expect(container.querySelector('hr')).not.toBeNull()
    expect(screen.getByRole('table')).toHaveTextContent('TinkerFin')
    expect(screen.getByRole('region', { name: '可横向滚动的表格' })).toHaveAttribute('tabindex', '0')
    expect(screen.getByRole('link', { name: 'TinkerFin 前端规范文档' })).toHaveAttribute(
      'href',
      'https://example.com/docs/frontend/visual-system/markdown-contract-and-accessibility-checklist',
    )
  })

  it('renders inline code separately from a language-labelled code block', () => {
    const { container } = render(<MarkdownContent content={completeFixture} />)

    expect(container.querySelector('.markdown-content p code')).toHaveTextContent('const answer = 42')
    expect(container.querySelector('.markdown-code-block__head')).toHaveTextContent('ts')
    expect(container.querySelector('.markdown-code-block pre code')).toHaveTextContent('const value = 42')
  })

  it('copies code through the real client-side clipboard action', async () => {
    const user = userEvent.setup()
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    render(<MarkdownContent content={completeFixture} />)

    await user.click(screen.getByRole('button', { name: '复制' }))
    expect(writeText).toHaveBeenCalledWith('const value = 42')
    expect(screen.getByRole('button', { name: '已复制' })).toBeInTheDocument()
  })

  it('exposes compact content as an explicit variant', () => {
    const { container } = render(<MarkdownContent content="**工具结果**" variant="compact" />)
    expect(container.firstElementChild).toHaveClass('markdown-content--compact')
  })
})
