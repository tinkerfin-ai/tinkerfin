import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { BrandLogo } from './BrandLogo'
import { BrandMark } from './BrandMark'

describe('BrandLogo', () => {
  it('按尺寸变体渲染同一组装饰性品牌资产', () => {
    const { container } = render(<BrandLogo size="md" className="custom-logo" />)
    const logo = container.querySelector('.brand-logo')
    const mark = container.querySelector('.brand-logo__mark')
    const wordmark = container.querySelector('.brand-logo__wordmark')

    expect(logo).toHaveClass('brand-logo--md', 'custom-logo')
    expect(logo).toHaveAttribute('aria-hidden', 'true')
    expect(mark).toHaveAttribute('src', '/brand/tinkerfin-mark.png?v=1')
    expect(mark).toHaveAttribute('width', '647')
    expect(mark).toHaveAttribute('height', '458')
    expect(mark).toHaveAttribute('alt', '')
    expect(wordmark).toHaveAttribute('src', '/brand/tinkerfin-wordmark.png?v=1')
    expect(wordmark).toHaveAttribute('width', '1295')
    expect(wordmark).toHaveAttribute('height', '242')
    expect(wordmark).toHaveAttribute('alt', '')
  })

  it('独立品牌图标按调用方指定的可见高度渲染', () => {
    const { container } = render(<BrandMark size={28} />)
    const mark = container.querySelector('.brand-mark')

    expect(mark).toHaveStyle({ '--brand-mark-height': '28px' })
    expect(mark?.querySelector('img')).toHaveAttribute('src', '/brand/tinkerfin-mark.png?v=1')
    expect(mark?.querySelector('img')).toHaveAttribute('alt', '')
  })
})
