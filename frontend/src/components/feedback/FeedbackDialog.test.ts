import { describe, it, expect } from 'vitest';
import { contextSummary, fromRoute } from './FeedbackDialog';

/**
 * The context is read from where the person already is, rather than asked for — COSMOS asks
 * "which project is this about?", which is the one question a learner in a broken lab cannot
 * answer. These cover the two halves of that: what the route alone yields, and what the
 * submitter is shown before they agree to send it.
 */

const RANGE = '0e8bb7fd-4b36-4b2e-a395-a98fb30c1bd7';

describe('what the route alone tells us', () => {
  it('finds the range a range page is about', () => {
    expect(fromRoute(`/ranges/${RANGE}`)).toMatchObject({ source: 'range', rangeId: RANGE });
  });

  it('finds it on a sub-route too', () => {
    expect(fromRoute(`/ranges/${RANGE}/console`).rangeId).toBe(RANGE);
  });

  it('claims no range when the path names none', () => {
    expect(fromRoute('/blueprints')).toMatchObject({ source: 'app', rangeId: null });
  });

  it('does not mistake some other id for a range', () => {
    expect(fromRoute(`/content/${RANGE}`).rangeId).toBeNull();
  });

  it('always records the page', () => {
    expect(fromRoute('/admin').context?.route).toBe('/admin');
  });
});

describe('what the submitter is shown before sending', () => {
  it('names the guide and the step when a page supplied them', () => {
    const lines = contextSummary({
      context: { content_title: 'Getting Started', step: 'Phase 3, step 5', route: '/lab' },
    });
    expect(lines[0]).toBe('Guide: Getting Started — Phase 3, step 5');
  });

  it('names the range by name when it has one', () => {
    const lines = contextSummary({ rangeId: RANGE, context: { range_name: 'web-lab' } });
    expect(lines).toContain('Range: web-lab');
  });

  it('never shows a raw id, because that is not something anyone can agree to', () => {
    const lines = contextSummary({ rangeId: RANGE, context: { route: `/ranges/${RANGE}` } });
    expect(lines.some((l) => l.includes(RANGE) && l.startsWith('Range'))).toBe(false);
    expect(lines).toContain('The range you are looking at');
  });

  it('is empty when there is nothing to attach, so no empty box is drawn', () => {
    expect(contextSummary({})).toEqual([]);
  });
});
