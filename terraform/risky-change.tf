resource "aws_security_group" "bastion" {
  name        = "bastion-sg"
  description = "SSH access"

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_iam_policy" "app" {
  name = "app-full-access"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "*"
      Resource = "*"
    }]
  })
}

resource "aws_db_instance" "orders" {
  identifier          = "orders-db"
  engine              = "postgres"
  instance_class      = "db.r6g.4xlarge"
  allocated_storage   = 100
  username            = "admin_user"
  password            = "SuperSecret123!"
  publicly_accessible = true
  storage_encrypted   = false
  skip_final_snapshot = true
  deletion_protection = false
}