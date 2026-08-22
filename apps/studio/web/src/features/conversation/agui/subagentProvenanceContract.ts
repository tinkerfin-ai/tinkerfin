import type { JsonObject } from "../../../types"

export const SUBAGENT_PROVENANCE_SCHEMA = "tinkerfin.subagent-provenance.v1" as const

export interface SubagentProvenance {
  readonly schema: typeof SUBAGENT_PROVENANCE_SCHEMA
  readonly subagentInvocationId: string
  readonly namespace: readonly string[]
  readonly parentNamespace: readonly string[]
  readonly graphTaskId: string
  readonly agentName: string
  readonly parentToolCallId: string
  readonly description: string
  readonly requestRunId: string
}

const KEYS = [
  "agentName",
  "description",
  "graphTaskId",
  "namespace",
  "parentNamespace",
  "parentToolCallId",
  "requestRunId",
  "schema",
  "subagentInvocationId",
] as const

export class SubagentProvenanceContractError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "SubagentProvenanceContractError"
  }
}

const isObject = (value: unknown): value is JsonObject =>
  Boolean(value && typeof value === "object" && !Array.isArray(value))

const canonicalText = (value: unknown): value is string =>
  typeof value === "string" && Boolean(value) && value.trim() === value

const namespace = (value: unknown, field: string): string[] => {
  if (!Array.isArray(value) || !value.every(canonicalText)) {
    throw new SubagentProvenanceContractError(`${field} 无效`)
  }
  return value
}

export const parseSubagentProvenance = (value: unknown): SubagentProvenance => {
  if (!isObject(value)) throw new SubagentProvenanceContractError("subagent provenance 必须是对象")
  const actualKeys = Object.keys(value).sort()
  const expectedKeys = [...KEYS].sort()
  if (
    actualKeys.length !== expectedKeys.length
    || !actualKeys.every((key, index) => key === expectedKeys[index])
  ) throw new SubagentProvenanceContractError("subagent provenance 字段不符合 v1 契约")
  const parentNamespace = namespace(value.parentNamespace, "parentNamespace")
  const childNamespace = namespace(value.namespace, "namespace")
  if (
    value.schema !== SUBAGENT_PROVENANCE_SCHEMA
    || !canonicalText(value.subagentInvocationId)
    || !/^subagent-[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value.subagentInvocationId)
    || !canonicalText(value.graphTaskId)
    || !canonicalText(value.agentName)
    || !canonicalText(value.parentToolCallId)
    || !canonicalText(value.description)
    || !canonicalText(value.requestRunId)
    || childNamespace.length !== parentNamespace.length + 1
    || !parentNamespace.every((part, index) => childNamespace[index] === part)
    || !childNamespace.at(-1)?.startsWith(`tools:${value.graphTaskId}`)
  ) throw new SubagentProvenanceContractError("subagent provenance 关联字段不一致")
  return {
    schema: SUBAGENT_PROVENANCE_SCHEMA,
    subagentInvocationId: value.subagentInvocationId,
    namespace: childNamespace,
    parentNamespace,
    graphTaskId: value.graphTaskId,
    agentName: value.agentName,
    parentToolCallId: value.parentToolCallId,
    description: value.description,
    requestRunId: value.requestRunId,
  }
}
