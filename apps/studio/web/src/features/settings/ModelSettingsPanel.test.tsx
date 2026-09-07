import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ModelOptionsEditorProps } from './ModelOptionsEditor'
import { ModelSettingsPanel } from './ModelSettingsPanel'
import { requestJson } from '../../api/shared/http'
import { newModel } from './useModelSettings'

vi.mock('../../api/shared/http', () => ({ requestJson: vi.fn() }))
vi.mock('./ModelOptionsEditor', () => ({ default: ({value, onChange}: ModelOptionsEditorProps) => <textarea aria-label="高级参数 JSON" value={value} onChange={(event) => onChange(event.target.value)} /> }))

describe('personal model settings', () => {
  beforeEach(() => { vi.mocked(requestJson).mockReset(); HTMLElement.prototype.scrollIntoView = vi.fn() })
  it('shows key presence without exposing a secret and saves an edited name', async () => {
    const model = {
      model_id: 'mine',
      display_name: 'Mine',
      purpose: 'chat',
      provider: 'openai',
      model_name: 'model',
      base_url: 'https://api.openai.com/v1',
      has_key: true,
      image_support: 'unknown',
      reasoning_enabled: false,
      enabled: true,
      is_default: true,
      sort_order: 0,
      generation_options: {},
    }
    vi.mocked(requestJson)
      .mockResolvedValueOnce([model])
      .mockResolvedValueOnce(null)
      .mockResolvedValueOnce([{ ...model, display_name: 'Updated' }])
    const changed = vi.fn()
    render(<ModelSettingsPanel onChanged={changed} />)
    await screen.findByText('Mine')
    expect(screen.queryByText('model', {exact: true})).not.toBeInTheDocument()
    expect(screen.getByText(/已配置密钥/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '编辑' }))
    expect(screen.getByLabelText('API Key')).toHaveValue('')
    fireEvent.change(screen.getByLabelText('显示名称'), {
      target: { value: 'Updated' },
    })
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(changed).toHaveBeenCalledOnce())
    expect(vi.mocked(requestJson).mock.calls[1][1]?.body).toMatchObject({
      display_name: 'Updated',
      api_key: '',
    })
  })
  it.each(['返回模型列表', '取消'])('preserves a failed draft until leaving with %s', async (leave) => {
    vi.mocked(requestJson).mockResolvedValueOnce([]).mockRejectedValueOnce(new Error('模型仍在运行，请结束后再修改'))
    render(<ModelSettingsPanel />)
    await screen.findByText('还没有模型配置，请先添加')
    fireEvent.click(screen.getByRole('button', { name: '添加模型' }))
    fireEvent.change(screen.getByLabelText('显示名称'), {target: {value: 'My model'}})
    fireEvent.change(screen.getByLabelText('Model ID'), {target: {value: 'provider-model'}})
    fireEvent.change(screen.getByLabelText('API Key'), {target: {value: 'secret'}})
    fireEvent.click(screen.getByRole('button', {name: '保存'}))
    await screen.findByRole('alert')
    expect(screen.getByRole('alert')).toHaveTextContent('模型仍在运行，请结束后再修改')
    expect(screen.getByLabelText('显示名称')).toHaveValue('My model')
    expect(screen.getByRole('button', {name: '保存'})).toBeEnabled()
    fireEvent.click(screen.getByRole('button', {name: leave}))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', {name: '添加模型'}))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByLabelText('API Key')).toHaveValue('')
  })

  it('uses the shared keyboard picker and restores focus', async () => {
    vi.mocked(requestJson).mockResolvedValueOnce([])
    render(<ModelSettingsPanel />)
    await screen.findByText('还没有模型配置，请先添加')
    fireEvent.click(screen.getByRole('button', {name: '添加模型'}))
    fireEvent.click(screen.getByRole('radio', {name: '图片生成'}))
    fireEvent.click(screen.getByRole('button', {name: '图片格式'}))
    const listbox = screen.getByRole('listbox', {name: '图片格式'})
    fireEvent.keyDown(listbox, {key: 'End'})
    fireEvent.keyDown(listbox, {key: 'Enter'})
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(screen.getByRole('button', {name: '图片格式'})).toHaveFocus()
    fireEvent.click(screen.getByText('高级参数', {exact: true}))
    await screen.findByLabelText('高级参数 JSON')
  })

  it('keeps the chat provider and reasoning selection when switching purpose', async () => {
    const model = {...newModel(), display_name: 'DeepSeek', model_name: 'deepseek-model', provider: 'deepseek', reasoning_enabled: true, has_key: true}
    vi.mocked(requestJson).mockResolvedValueOnce([model]).mockResolvedValueOnce(null).mockResolvedValueOnce([model])
    render(<ModelSettingsPanel />)
    await screen.findByText('DeepSeek')
    fireEvent.click(screen.getByRole('button', {name: '编辑'}))
    fireEvent.click(screen.getByRole('radio', {name: '图片生成'}))
    fireEvent.click(screen.getByRole('radio', {name: '对话模型'}))
    expect(screen.getByRole('button', {name: '接口类型'})).toHaveTextContent('DeepSeek')
    expect(screen.getByRole('checkbox', {name: '启用推理'})).toBeChecked()
    fireEvent.click(screen.getByRole('button', {name: '保存'}))
    await waitFor(() => expect(vi.mocked(requestJson).mock.calls[1]?.[1]?.body).toMatchObject({provider: 'deepseek', reasoning_enabled: true}))
  })

  it.each([true, false])('sets the default directly for an enabled=%s model', async (enabled) => {
    const model = {...newModel(), display_name: 'Mine', has_key: true, enabled}
    vi.mocked(requestJson).mockResolvedValueOnce([model]).mockResolvedValueOnce(null).mockResolvedValueOnce([{...model, enabled: true, is_default: true}])
    const changed = vi.fn()
    render(<ModelSettingsPanel onChanged={changed} />)
    fireEvent.click(await screen.findByRole('button', {name: enabled ? '设为默认' : '启用并设为默认'}))
    await screen.findByText('默认', {exact: true})
    const [path, request] = vi.mocked(requestJson).mock.calls[1]
    expect(path).toBe(`/api/models/configurations/${model.model_id}/default`)
    expect(request?.method).toBe('PUT')
    expect(request?.body).toBeUndefined()
    expect(screen.queryByLabelText('API Key')).not.toBeInTheDocument()
    expect(changed).toHaveBeenCalledOnce()
  })

  it('requires a key before choosing a default', async () => {
    vi.mocked(requestJson).mockResolvedValueOnce([{...newModel(), display_name: 'Missing key'}])
    render(<ModelSettingsPanel />)
    expect(await screen.findByRole('button', {name: '设为默认'})).toBeDisabled()
    expect(screen.getByRole('button', {name: '设为默认'})).toHaveAttribute('title', '请先配置密钥')
    expect(requestJson).toHaveBeenCalledOnce()
  })

  it('requires a new key for a different address and accepts the saved endpoint again', async () => {
    const model = {...newModel(), display_name: 'Mine', model_name: 'example', has_key: true}
    vi.mocked(requestJson).mockResolvedValueOnce([model])
    render(<ModelSettingsPanel />)
    fireEvent.click(await screen.findByRole('button', {name: '编辑'}))
    const key = screen.getByLabelText('API Key')
    expect(key).not.toBeRequired()
    fireEvent.change(screen.getByLabelText('Base URL'), {target: {value: 'https://different.example/v1'}})
    expect(key).toBeRequired()
    expect(key).toHaveAttribute('placeholder', '服务地址已更改，请重新输入 API Key')
    fireEvent.click(screen.getByRole('button', {name: '保存'}))
    expect(requestJson).toHaveBeenCalledOnce()
    fireEvent.change(screen.getByLabelText('Base URL'), {target: {value: 'https://API.OPENAI.COM:443/v1/'}})
    expect(key).not.toBeRequired()
    expect(key).toHaveAttribute('placeholder', '留空保留已保存的密钥')
    fireEvent.change(screen.getByLabelText('Base URL'), {target: {value: 'https://api.openai.com/v2'}})
    expect(key).toBeRequired()
    fireEvent.change(screen.getByLabelText('Base URL'), {target: {value: 'invalid'}})
    expect(key).toBeRequired()
  })

  it('preserves the list and allows another default attempt after failure', async () => {
    const model = {...newModel(), display_name: 'Mine', has_key: true}
    vi.mocked(requestJson).mockResolvedValueOnce([model]).mockRejectedValueOnce(new Error('服务暂不可用')).mockResolvedValueOnce(null).mockResolvedValueOnce([{...model, is_default: true}])
    render(<ModelSettingsPanel />)
    fireEvent.click(await screen.findByRole('button', {name: '设为默认'}))
    expect(await screen.findByRole('alert')).toHaveTextContent('服务暂不可用')
    expect(screen.getByText('Mine')).toBeVisible()
    fireEvent.click(screen.getByRole('button', {name: '设为默认'}))
    await screen.findByText('默认', {exact: true})
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
