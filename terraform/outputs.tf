output "service_url" {
  description = "The root URL of the deployed Cloud Run service."
  value       = google_cloud_run_v2_service.connector.uri
}

output "mcp_endpoint_url" {
  description = "The Streamable HTTP MCP Endpoint URL for CX Agent Studio Toolset configuration."
  value       = "${google_cloud_run_v2_service.connector.uri}/mcp"
}

output "relay_api_url" {
  description = "The Relay API base URL for live human chat escalation."
  value       = "${google_cloud_run_v2_service.connector.uri}/api"
}
