output "instance_id" {
  description = "EC2 instance ID."
  value       = aws_instance.app.id
}

output "public_ip" {
  description = "Public IPv4 address of the demo instance."
  value       = aws_instance.app.public_ip
}

output "public_dns" {
  description = "Public DNS name of the demo instance."
  value       = aws_instance.app.public_dns
}

output "dashboard_url" {
  description = "URL for the ops dashboard once Compose has finished starting."
  value       = "http://${aws_instance.app.public_ip}:8000/"
}

output "security_group_id" {
  description = "Security group attached to the instance."
  value       = aws_security_group.app.id
}

output "openai_ssm_parameter_name" {
  description = "SSM parameter the instance reads for OPENAI_API_KEY."
  value       = local.openai_parameter_path
}

output "ssh_example" {
  description = "Example SSH command when key_name was set."
  value       = var.key_name != "" ? "ssh -i <path-to-key.pem> ec2-user@${aws_instance.app.public_ip}" : "No key_name set; use AWS SSM Session Manager instead."
}
