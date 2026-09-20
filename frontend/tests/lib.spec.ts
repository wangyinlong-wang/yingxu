import { describe, expect, it } from 'vitest'
import { ApiError, formatBytes, formatDate, isConflict } from '../src/lib'
describe('API presentation helpers', () => { it('identifies only optimistic concurrency conflicts', () => { expect(isConflict(new ApiError(409,{code:'VERSION_CONFLICT',message:'stale'}))).toBe(true); expect(isConflict(new ApiError(400,{code:'BAD_REQUEST',message:'bad'}))).toBe(false) }); it('formats safe empty and file values', () => { expect(formatBytes(1024 * 1024)).toBe('1.0 MB'); expect(formatDate(null)).toBe('—') }) })
