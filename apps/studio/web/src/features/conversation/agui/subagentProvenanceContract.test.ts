import { describe, expect, it } from "vitest"

import fixture from "./contracts/subagent-provenance.fixture.json"
import {
  SUBAGENT_PROVENANCE_SCHEMA,
  SubagentProvenanceContractError,
  parseSubagentProvenance,
} from "./subagentProvenanceContract"

describe("Subagent provenance current contract", () => {
  it("parses the generated cross-language fixture", () => {
    expect(parseSubagentProvenance(fixture)).toEqual(fixture)
    expect(fixture.schema).toBe(SUBAGENT_PROVENANCE_SCHEMA)
  })

  it.each([
    ["missing schema", (() => {
      return Object.fromEntries(
        Object.entries(fixture).filter(([key]) => key !== "schema"),
      )
    })()],
    ["unknown field", { ...fixture, unknown: true }],
    ["wrong graphNamespace", { ...fixture, graphNamespace: ["tools:other"] }],
    ["wrong graph task", { ...fixture, graphTaskId: "other" }],
    ["invalid invocation ID", { ...fixture, subagentInvocationId: "subagent-invalid" }],
    ["empty request run", { ...fixture, requestRunId: "" }],
  ])("rejects %s", (_name, value) => {
    expect(() => parseSubagentProvenance(value)).toThrow(SubagentProvenanceContractError)
  })
})
