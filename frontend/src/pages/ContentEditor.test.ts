import { describe, it, expect } from 'vitest';
import { htmlToMarkdown, markdownAsPlainHtml } from './ContentEditor';

/**
 * The editor stores markdown and the API re-renders the HTML it reloads, so a
 * serialiser that cannot express something destroys it on save and hands the
 * author back the wreckage. The previous one was a chain of regexes: `<code>`
 * had no `s` flag so a multi-line block fell through to the catch-all tag
 * strip, every `<li>` became a `-` bullet, and `<table>`, `<pre>` and `<s>` had
 * no rule at all -- four of the toolbar's own buttons. These cover those four,
 * the escaping that stops prose being re-read as syntax, and the refusal that
 * replaces a silent save for the constructs markdown genuinely cannot hold.
 *
 * The HTML in each case is TipTap's own serialisation, which is the only input
 * this ever sees.
 */

describe('htmlToMarkdown', () => {
  it('keeps a table as a pipe table', () => {
    const html =
      '<table><tbody>' +
      '<tr><th colspan="1" rowspan="1"><p>Step</p></th><th colspan="1" rowspan="1"><p>Command</p></th></tr>' +
      '<tr><td colspan="1" rowspan="1"><p>1</p></td><td colspan="1" rowspan="1"><p>ls -la</p></td></tr>' +
      '</tbody></table>';
    expect(htmlToMarkdown(html)).toEqual({
      markdown: '| Step | Command |\n| --- | --- |\n| 1 | ls -la |',
      losses: [],
    });
  });

  it('keeps every line of a code block, with its language', () => {
    const html =
      '<pre><code class="language-bash">cd /srv\ndocker compose up -d\necho &quot;done&quot;</code></pre>';
    expect(htmlToMarkdown(html).markdown).toBe(
      '```bash\ncd /srv\ndocker compose up -d\necho "done"\n```'
    );
  });

  it('numbers an ordered list instead of bulleting it', () => {
    const html = '<ol><li><p>First</p></li><li><p>Second</p></li></ol>';
    expect(htmlToMarkdown(html).markdown).toBe('1. First\n2. Second');
  });

  it('keeps strikethrough, which the renderer has no markdown syntax for', () => {
    const html = '<p>This is <s>wrong</s> and this is right</p>';
    expect(htmlToMarkdown(html).markdown).toBe('This is <s>wrong</s> and this is right');
  });

  it('keeps underline, which StarterKit binds to Mod-U with no toolbar button', () => {
    const html = '<p>Press <u>Enter</u> now</p>';
    expect(htmlToMarkdown(html).markdown).toBe('Press <u>Enter</u> now');
  });

  it('indents a code block nested in a list, where a fence does not survive', () => {
    // Python-Markdown's fenced_code only matches at the outermost level; behind
    // a list indent it reads the fence as an inline code span and folds the
    // block into a paragraph. The indented form nests.
    const html =
      '<ol><li><p>Run it</p><pre><code class="language-bash">./install.sh</code></pre></li></ol>';
    expect(htmlToMarkdown(html).markdown).toBe('1. Run it\n\n        ./install.sh');
  });

  it('separates list items that carry a nested block', () => {
    // Without the blank line the next item, which sits at column zero, is read
    // as part of the previous item's nested list.
    const html = '<ul><li><p>outer</p><ul><li><p>inner</p></li></ul></li><li><p>second</p></li></ul>';
    expect(htmlToMarkdown(html).markdown).toBe('- outer\n\n    - inner\n\n- second');
  });

  it('stops prose being re-read as markdown syntax', () => {
    const html =
      '<p># not a heading</p><p>- not a bullet</p><p>1. not numbered</p>' +
      '<p>2 * 3 and 5 &gt; 3 &amp; 2 &lt; 4</p><p>call body_markdown</p>';
    expect(htmlToMarkdown(html).markdown).toBe(
      '\\# not a heading\n\n\\- not a bullet\n\n1\\. not numbered\n\n' +
        '2 \\* 3 and 5 &gt; 3 &amp; 2 &lt; 4\n\ncall body_markdown'
    );
  });

  it('escapes a pipe in cell text but not one inside a code span', () => {
    const html =
      '<table><tbody><tr><th><p>a|b</p></th><th><p>x</p></th></tr>' +
      '<tr><td><p><code>ls | wc</code></p></td><td><p>y</p></td></tr></tbody></table>';
    expect(htmlToMarkdown(html).markdown).toBe(
      '| a\\|b | x |\n| --- | --- |\n| `ls | wc` | y |'
    );
  });

  it('keeps a hard break as a hard break', () => {
    expect(htmlToMarkdown('<p>line one<br>line two</p>').markdown).toBe('line one  \nline two');
  });

  it('does not hand text back to the renderer as live markup', () => {
    const html = '<p>Type &lt;script&gt;alert(1)&lt;/script&gt; into the box</p>';
    expect(htmlToMarkdown(html).markdown).toBe(
      'Type &lt;script&gt;alert(1)&lt;/script&gt; into the box'
    );
  });
});

describe('htmlToMarkdown loss reporting', () => {
  it('refuses a merged cell rather than dropping it', () => {
    const html =
      '<table><tbody><tr><th colspan="2" rowspan="1"><p>Wide</p></th></tr>' +
      '<tr><td><p>a</p></td><td><p>b</p></td></tr></tbody></table>';
    expect(htmlToMarkdown(html).losses).toEqual([
      'a merged table cell, which a markdown table cannot span across rows or columns',
    ]);
  });

  it('refuses a block inside a table cell', () => {
    const html =
      '<table><tbody><tr><th><p>a</p></th><th><p>b</p></th></tr>' +
      '<tr><td><pre><code>ls</code></pre></td><td><p>b</p></td></tr></tbody></table>';
    expect(htmlToMarkdown(html).losses).toEqual([
      'a code block inside a table cell, where markdown allows only one line of text',
    ]);
  });

  it('refuses a list whose numbering markdown would reset', () => {
    const html = '<ol start="3"><li><p>three</p></li></ol>';
    expect(htmlToMarkdown(html).losses).toEqual([
      'a numbered list starting at 3, which markdown renumbers from 1',
    ]);
  });

  it('reports nothing for content markdown can hold', () => {
    const html =
      '<h2>Recovery</h2><p>Run <code>systemctl status</code>, see <a href="/d">notes</a>.</p>' +
      '<ul><li><p>one</p></li></ul><blockquote><p>care</p></blockquote>' +
      '<pre><code class="language-sh">ls</code></pre>' +
      '<table><tbody><tr><th><p>a</p></th></tr><tr><td><p>1</p></td></tr></tbody></table>';
    expect(htmlToMarkdown(html).losses).toEqual([]);
  });
});

describe('markdownAsPlainHtml', () => {
  it('shows a body with no cached render as text, not as markup', () => {
    // This branch used to interpolate the stored markdown straight into `<p>`
    // tags, which handed any HTML in it to the browser as live markup.
    expect(markdownAsPlainHtml('a <b>bold</b> & <img src=x onerror=alert(1)>')).toBe(
      '<p>a &lt;b&gt;bold&lt;/b&gt; &amp; &lt;img src=x onerror=alert(1)&gt;</p>'
    );
  });

  it('keeps paragraph breaks apart from line breaks', () => {
    expect(markdownAsPlainHtml('one\ntwo\n\nthree')).toBe('<p>one<br>two</p><p>three</p>');
  });
});
