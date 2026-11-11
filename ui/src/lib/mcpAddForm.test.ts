import { describe, expect, it } from 'vitest';
import { buildMcpFormJson, parseMcpTarget } from './mcpAddForm';

describe('parseMcpTarget', () => {
  it.each([
    ['npx -y @modelcontextprotocol/server-everything', { kind: 'stdio', command: 'npx', args: ['-y', '@modelcontextprotocol/server-everything'] }],
    ['  uvx mcp-server-fetch  ', { kind: 'stdio', command: 'uvx', args: ['mcp-server-fetch'] }],
    ['docker run -i --rm -e KEY=VAL img', { kind: 'stdio', command: 'docker', args: ['run', '-i', '--rm', '-e', 'KEY=VAL', 'img'] }],
    ['node "/tmp/my server.js" --name \'a b\'', { kind: 'stdio', command: 'node', args: ['/tmp/my server.js', '--name', 'a b'] }],
    ['tool "" --x', { kind: 'stdio', command: 'tool', args: ['', '--x'] }],
    ['echo $HOME', { kind: 'stdio', command: 'echo', args: ['$HOME'] }],
    ['tool "unclosed', { kind: 'error', issue: 'unclosedQuote' }],
    ['cat x | tool', { kind: 'error', issue: 'shellOperator' }],
    ['cd /tmp && tool', { kind: 'error', issue: 'shellOperator' }],
    ['tool "a | b"', { kind: 'stdio', command: 'tool', args: ['a | b'] }],
    ['C:\\tools\\server.exe --port 1', { kind: 'error', issue: 'windowsPath' }],
    ["node 'relative\\server.js'", { kind: 'error', issue: 'windowsPath' }],
    ['https://example.com/mcp', { kind: 'http', url: 'https://example.com/mcp' }],
    ['https://exa mple.com/mcp', { kind: 'error', issue: 'invalidUrl' }],
    ['http://', { kind: 'error', issue: 'invalidUrl' }],
    ['https://example.com/sse', { kind: 'http', url: 'https://example.com/sse' }],
    ['mcp.linear.app/mcp', { kind: 'stdio', command: 'mcp.linear.app/mcp', args: [], hint: 'missingScheme' }],
    ['venv.3/bin/mcp-server', { kind: 'stdio', command: 'venv.3/bin/mcp-server', args: [] }],
    ['mcp.tools/server', { kind: 'stdio', command: 'mcp.tools/server', args: [] }],
    ['   ', { kind: 'empty' }],
  ])('%s', (input, expected) => {
    expect(parseMcpTarget(input)).toEqual(expected);
  });
});

describe('buildMcpFormJson', () => {
  it('sends only the detected kind rows, drops blank keys and omits empty sections', () => {
    expect(JSON.parse(buildMcpFormJson({
      name: ' docs ', target: { kind: 'http', url: 'https://example.com/mcp' }, headerRows: [{ key: ' ', value: 'x' }], envRows: [{ key: 'SECRET', value: 's' }], cwd: '/ignored',
    }))).toEqual({ mcpServers: { docs: { type: 'http', url: 'https://example.com/mcp' } } });
  });
});
