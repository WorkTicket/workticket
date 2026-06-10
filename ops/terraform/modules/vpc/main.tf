variable "environment" { type = string }
variable "vpc_cidr" { type = string }
variable "azs" { type = list(string) }
variable "private_subnets" { type = list(string) }
variable "public_subnets" { type = list(string) }
variable "database_subnets" { type = list(string) }

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = { Name = "workticket-${var.environment}-vpc" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "workticket-${var.environment}-igw" }
}

resource "aws_subnet" "private" {
  count             = length(var.private_subnets)
  vpc_id            = aws_vpc.this.id
  cidr_block        = var.private_subnets[count.index]
  availability_zone = var.azs[count.index]
  tags              = { Name = "workticket-${var.environment}-private-${count.index}", Type = "private" }
}

resource "aws_subnet" "public" {
  count                   = length(var.public_subnets)
  vpc_id                  = aws_vpc.this.id
  cidr_block              = var.public_subnets[count.index]
  availability_zone       = var.azs[count.index]
  map_public_ip_on_launch = true
  tags                    = { Name = "workticket-${var.environment}-public-${count.index}", Type = "public" }
}

resource "aws_subnet" "database" {
  count             = length(var.database_subnets)
  vpc_id            = aws_vpc.this.id
  cidr_block        = var.database_subnets[count.index]
  availability_zone = var.azs[count.index]
  tags              = { Name = "workticket-${var.environment}-database-${count.index}", Type = "database" }
}

resource "aws_eip" "nat" {
  count  = length(var.azs)
  domain = "vpc"
  tags   = { Name = "workticket-${var.environment}-nat-eip-${count.index}" }
}

resource "aws_nat_gateway" "this" {
  count         = length(var.azs)
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
  tags          = { Name = "workticket-${var.environment}-nat-gw-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = { Name = "workticket-${var.environment}-public-rt" }
}

resource "aws_route_table" "private" {
  count  = length(var.azs)
  vpc_id = aws_vpc.this.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this[count.index].id
  }

  tags = { Name = "workticket-${var.environment}-private-rt-${count.index}" }
}

resource "aws_route_table_association" "public" {
  count          = length(var.public_subnets)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count          = length(var.private_subnets)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

resource "aws_route_table_association" "database" {
  count          = length(var.database_subnets)
  subnet_id      = aws_subnet.database[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

output "vpc_id"               { value = aws_vpc.this.id }
output "private_subnet_ids"   { value = aws_subnet.private[*].id }
output "public_subnet_ids"    { value = aws_subnet.public[*].id }
output "database_subnet_ids"  { value = aws_subnet.database[*].id }
output "private_subnet_cidrs" { value = aws_subnet.private[*].cidr_block }
