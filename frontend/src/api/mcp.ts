import { aiApi } from './aiClient';

export interface McpToolSummary {
  name: string;
  server: string;
  qualified_name: string;
  description: string;
  read_only: boolean;
  destructive: boolean;
  input_schema: JsonSchema;
}

export interface JsonSchema {
  type?: string;
  properties?: Record<string, JsonSchemaProperty>;
  required?: string[];
}

export interface JsonSchemaProperty {
  type?: string | string[];
  description?: string;
  default?: unknown;
  anyOf?: { type?: string }[];
}

export interface McpServerSummary {
  name: string;
  url: string;
  reachable: boolean;
  tool_count: number;
  error: string | null;
}

export interface McpToolsResponse {
  total_tools: number;
  servers: McpServerSummary[];
  tools: McpToolSummary[];
}

export interface McpCallResponse {
  tool: string;
  server: string;
  success: boolean;
  latency_ms: number;
  content: unknown;
  error: string | null;
}

export const mcpApi = {
  tools: (refresh = false) =>
    aiApi
      .get<McpToolsResponse>('/mcp/tools', { params: refresh ? { refresh: true } : undefined })
      .then((r) => r.data),

  call: (tool: string, args: Record<string, unknown>) =>
    aiApi
      .post<McpCallResponse>('/mcp/call', { tool, arguments: args })
      .then((r) => r.data),
};

export const mcpQueryKeys = {
  tools: ['mcp', 'tools'] as const,
};
