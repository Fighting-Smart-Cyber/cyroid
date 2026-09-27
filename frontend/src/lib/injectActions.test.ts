// frontend/src/lib/injectActions.test.ts
import { describe, expect, it } from 'vitest'
import {
  describeInjectAction,
  describeInjectActions,
  formatInjectTime,
  injectRefusalText,
} from './injectActions'

describe('describeInjectAction', () => {
  it('reads the shape the MSEL parser actually stores', () => {
    const stored = {
      action_type: 'run_command',
      parameters: { target_vm: 'dc01', command: 'net user /add' },
    }
    expect(describeInjectAction(stored)).toEqual({
      label: 'Run command',
      target: 'dc01',
      command: 'net user /add',
      path: null,
      filename: null,
    })
  })

  it('reads a place_file action with its parameters', () => {
    const stored = {
      action_type: 'place_file',
      parameters: { filename: 'orders.pdf', target_vm: 'ws01', target_path: 'C:\\Users' },
    }
    expect(describeInjectAction(stored)).toMatchObject({
      label: 'Place file',
      target: 'ws01',
      path: 'C:\\Users',
      filename: 'orders.pdf',
    })
  })

  it('still reads the flat shape the frontend type declared', () => {
    const flat = { type: 'place_file', target_vm: 'ws01', path: '/tmp/note.txt' }
    expect(describeInjectAction(flat)).toMatchObject({
      label: 'Place file',
      target: 'ws01',
      path: '/tmp/note.txt',
    })
  })

  it('labels an action type it has never seen rather than throwing', () => {
    expect(describeInjectAction({ action_type: 'cut_network' }).label).toBe('Cut network')
  })

  it('survives an action with no type, no parameters and no shape at all', () => {
    for (const junk of [null, undefined, 'run_command', 42, [], {}, { parameters: 'nope' }]) {
      const view = describeInjectAction(junk)
      expect(view.label).toBe('Action')
      expect(view.target).toBeNull()
    }
  })

  it('treats a blank string as a missing field', () => {
    const view = describeInjectAction({ action_type: 'run_command', parameters: { target_vm: '  ' } })
    expect(view.target).toBeNull()
  })
})

describe('describeInjectActions', () => {
  it('returns nothing for a field that is absent or not a list', () => {
    expect(describeInjectActions(undefined)).toEqual([])
    expect(describeInjectActions(null)).toEqual([])
    expect(describeInjectActions({ 0: 'x' })).toEqual([])
  })

  it('maps every entry it is given', () => {
    expect(describeInjectActions([{ action_type: 'run_command' }, {}])).toHaveLength(2)
  })
})

describe('injectRefusalText', () => {
  it('reads the substrate refusal the service returns whole', () => {
    // InjectService._refuse: {"success": false, "results": [{"error": reason}]}
    const results = [{ error: 'This install runs on the Kubernetes substrate...' }]
    expect(injectRefusalText(results)).toBe('This install runs on the Kubernetes substrate...')
  })

  it('reads an error nested under a per-action result', () => {
    // The normal loop: {"action": ..., "result": {"error": ...}}
    const results = [
      { action: { action_type: 'run_command' }, result: { exit_code: 0, output: 'ok' } },
      { action: { action_type: 'place_file' }, result: { error: 'Placing a file is not implemented' } },
    ]
    expect(injectRefusalText(results)).toBe('Placing a file is not implemented')
  })

  it('reads an error raised while an action ran', () => {
    expect(injectRefusalText([{ action: {}, error: 'boom' }])).toBe('boom')
  })

  it('returns nothing when every action succeeded or the body is not a list', () => {
    expect(injectRefusalText([{ action: {}, result: { exit_code: 0 } }])).toBeNull()
    expect(injectRefusalText(undefined)).toBeNull()
    expect(injectRefusalText('refused')).toBeNull()
    expect(injectRefusalText([null, 7, 'x'])).toBeNull()
    expect(injectRefusalText([{ error: '   ' }])).toBeNull()
  })
})

describe('formatInjectTime', () => {
  it('formats minutes from exercise start', () => {
    expect(formatInjectTime(0)).toBe('T+00:00')
    expect(formatInjectTime(9)).toBe('T+00:09')
    expect(formatInjectTime(125)).toBe('T+02:05')
  })

  it('says it does not know rather than printing NaN', () => {
    for (const bad of [undefined, null, 'soon', NaN, Infinity, -5]) {
      expect(formatInjectTime(bad)).toBe('T+--:--')
    }
  })
})
