"""Generates self-contained reference repository for TCB verification."""
from __future__ import annotations

from pathlib import Path
from core.context_validator.models import BenchmarkCase


def populate_benchmark_repo(root: Path) -> list[BenchmarkCase]:
    (root / "src" / "controllers").mkdir(parents=True, exist_ok=True)
    (root / "src" / "services").mkdir(parents=True, exist_ok=True)
    (root / "src" / "repositories").mkdir(parents=True, exist_ok=True)
    (root / "src" / "models").mkdir(parents=True, exist_ok=True)
    (root / "src" / "middleware").mkdir(parents=True, exist_ok=True)

    # 1. Controller
    (root / "src" / "controllers" / "attendance.controller.ts").write_text("""
import { AttendanceService } from '../services/attendance.service';
export class AttendanceController {
  async recordPunch(req: any, res: any) {
    const service = new AttendanceService();
    return await service.processPunch(req.user.id, req.body.timestamp);
  }
}
router.post('/api/attendance/punch', AttendanceController.recordPunch);
""")

    # 2. Service
    (root / "src" / "services" / "attendance.service.ts").write_text("""
import { AttendanceRepository } from '../repositories/attendance.repository';
export class AttendanceService {
  async processPunch(userId: string, timestamp: string) {
    const repo = new AttendanceRepository();
    const isLate = this.calculateLateArrival(timestamp);
    return await repo.createPunch({ userId, timestamp, isLate });
  }
  calculateLateArrival(punchTime: string): boolean {
    return new Date(punchTime).getHours() >= 10;
  }
}
""")

    # 3. Repository
    (root / "src" / "repositories" / "attendance.repository.ts").write_text("""
import { AttendanceModel } from '../models/attendance.model';
export class AttendanceRepository {
  async createPunch(data: any) {
    return await AttendanceModel.create(data);
  }
}
""")

    # 4. Model
    (root / "src" / "models" / "attendance.model.ts").write_text("""
export class AttendanceModel {
  static tableName = 'attendance_records';
  static async create(data: any) { return data; }
}
""")

    # 5. Auth Middleware
    (root / "src" / "middleware" / "auth.middleware.ts").write_text("""
export function verifyAuthToken(req: any, res: any, next: any) {
  const token = req.headers.authorization;
  if (!token) throw new Error('Unauthorized');
  next();
}
""")

    # Ground truth cases
    return [
        BenchmarkCase(
            query="attendance punch route and recording",
            expected_files=["attendance.controller.ts", "attendance.service.ts"],
            expected_symbols=["AttendanceController", "AttendanceService"],
            expected_flow=["recordPunch", "processPunch", "createPunch"],
            description="Punch entrypoint to repository flow",
        ),
        BenchmarkCase(
            query="calculate late arrival punch check",
            expected_files=["attendance.service.ts"],
            expected_symbols=["calculateLateArrival"],
            description="Business rule for late punches",
        ),
        BenchmarkCase(
            query="attendance model database persistence table",
            expected_files=["attendance.model.ts", "attendance.repository.ts"],
            expected_symbols=["AttendanceModel", "AttendanceRepository"],
            description="Database model mapping",
        ),
        BenchmarkCase(
            query="verify authorization token middleware",
            expected_files=["auth.middleware.ts"],
            expected_symbols=["verifyAuthToken"],
            description="Security authentication check",
        ),
    ]
