import { CHUNK_GEN_MEMORY, isSupersededGen, noteChunkGen } from './transcript'

/**
 * The gateway-generation order the chunk reducers keep (`ChatState.chunkGenOrder`).
 * Generations are random per process, so the order is the order the tab first
 * saw them in; a generation older than the newest one seen is superseded.
 */
describe('noteChunkGen / isSupersededGen', () => {
  it('orders generations by first sight and ignores a frame with no generation', () => {
    const order: string[] = []
    noteChunkGen(order, 'a')
    noteChunkGen(order, 'b')
    noteChunkGen(order, 'a')
    noteChunkGen(order, undefined)
    expect(order).toEqual(['a', 'b'])
    expect(isSupersededGen(order, 'a')).toBe(true)
    expect(isSupersededGen(order, 'b')).toBe(false)
    expect(isSupersededGen(order, 'c')).toBe(false)
    expect(isSupersededGen(order, undefined)).toBe(false)
    expect(isSupersededGen(undefined, 'a')).toBe(false)
  })

  it('forgets the oldest generation once the memory is full', () => {
    const order: string[] = []
    noteChunkGen(order, 'a')
    for (let i = 0; i < CHUNK_GEN_MEMORY; i++) noteChunkGen(order, `n${i}`)
    expect(order).toHaveLength(CHUNK_GEN_MEMORY)
    expect(order).not.toContain('a')
    expect(isSupersededGen(order, 'a')).toBe(false)
    expect(isSupersededGen(order, 'n0')).toBe(true)
  })
})
