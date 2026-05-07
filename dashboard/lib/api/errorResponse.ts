import { NextResponse } from "next/server";

type ErrorCode =
  | "INTERNAL_ERROR"
  | "UNAUTHORIZED"
  | "BAD_REQUEST"
  | "NOT_FOUND";

export function errorResponse(
  message: string,
  status: number,
  code: ErrorCode,
  requestId?: string,
): NextResponse {
  return NextResponse.json(
    {
      error: {
        code,
        message,
        ...(requestId ? { request_id: requestId } : {}),
      },
    },
    { status },
  );
}
