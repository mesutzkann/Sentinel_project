using System;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Sentinel.Infrastructure.Persistence.Migrations
{
    /// <inheritdoc />
    public partial class InitialCreate : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.EnsureSchema(
                name: "sentinel");

            migrationBuilder.CreateTable(
                name: "evaluation_runs",
                schema: "sentinel",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    Kind = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    ModelOrConfig = table.Column<string>(type: "character varying(200)", maxLength: 200, nullable: false),
                    StartedAt = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false),
                    CompletedAt = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: true),
                    Metrics = table.Column<string>(type: "jsonb", nullable: true),
                    Notes = table.Column<string>(type: "character varying(2000)", maxLength: 2000, nullable: true)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_evaluation_runs", x => x.Id);
                });

            migrationBuilder.CreateTable(
                name: "services",
                schema: "sentinel",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    Name = table.Column<string>(type: "character varying(100)", maxLength: 100, nullable: false),
                    DisplayName = table.Column<string>(type: "character varying(200)", maxLength: 200, nullable: false),
                    RepoPath = table.Column<string>(type: "character varying(500)", maxLength: 500, nullable: true),
                    HealthUrl = table.Column<string>(type: "character varying(500)", maxLength: 500, nullable: true),
                    MetricsJob = table.Column<string>(type: "character varying(100)", maxLength: 100, nullable: true),
                    CreatedAt = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_services", x => x.Id);
                });

            migrationBuilder.CreateTable(
                name: "users",
                schema: "sentinel",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    Username = table.Column<string>(type: "character varying(100)", maxLength: 100, nullable: false),
                    PasswordHash = table.Column<string>(type: "character varying(200)", maxLength: 200, nullable: false),
                    Role = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    CreatedAt = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_users", x => x.Id);
                });

            migrationBuilder.CreateTable(
                name: "evaluation_results",
                schema: "sentinel",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    RunId = table.Column<Guid>(type: "uuid", nullable: false),
                    CaseId = table.Column<string>(type: "character varying(100)", maxLength: 100, nullable: false),
                    Expected = table.Column<string>(type: "jsonb", nullable: true),
                    Actual = table.Column<string>(type: "jsonb", nullable: true),
                    Passed = table.Column<bool>(type: "boolean", nullable: false),
                    Details = table.Column<string>(type: "jsonb", nullable: true)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_evaluation_results", x => x.Id);
                    table.ForeignKey(
                        name: "FK_evaluation_results_evaluation_runs_RunId",
                        column: x => x.RunId,
                        principalSchema: "sentinel",
                        principalTable: "evaluation_runs",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "incidents",
                schema: "sentinel",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    IncidentCode = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false, defaultValueSql: "sentinel.next_incident_code()"),
                    ServiceId = table.Column<Guid>(type: "uuid", nullable: false),
                    Title = table.Column<string>(type: "character varying(300)", maxLength: 300, nullable: false),
                    Description = table.Column<string>(type: "character varying(4000)", maxLength: 4000, nullable: true),
                    Severity = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    Status = table.Column<string>(type: "character varying(30)", maxLength: 30, nullable: false),
                    StartedAt = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false),
                    ResolvedAt = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: true),
                    CreatedBy = table.Column<Guid>(type: "uuid", nullable: true),
                    CreatedAt = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_incidents", x => x.Id);
                    table.ForeignKey(
                        name: "FK_incidents_services_ServiceId",
                        column: x => x.ServiceId,
                        principalSchema: "sentinel",
                        principalTable: "services",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Restrict);
                    table.ForeignKey(
                        name: "FK_incidents_users_CreatedBy",
                        column: x => x.CreatedBy,
                        principalSchema: "sentinel",
                        principalTable: "users",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.SetNull);
                });

            migrationBuilder.CreateTable(
                name: "investigations",
                schema: "sentinel",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    IncidentId = table.Column<Guid>(type: "uuid", nullable: false),
                    Query = table.Column<string>(type: "character varying(2000)", maxLength: 2000, nullable: false),
                    RouterIntent = table.Column<string>(type: "character varying(64)", maxLength: 64, nullable: true),
                    RouterOutput = table.Column<string>(type: "jsonb", nullable: true),
                    Status = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    StartedAt = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false),
                    CompletedAt = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: true),
                    TotalDurationMs = table.Column<int>(type: "integer", nullable: true),
                    LlmCalls = table.Column<int>(type: "integer", nullable: false),
                    ToolCalls = table.Column<int>(type: "integer", nullable: false),
                    PromptTokens = table.Column<int>(type: "integer", nullable: false),
                    CompletionTokens = table.Column<int>(type: "integer", nullable: false),
                    FailureReason = table.Column<string>(type: "character varying(1000)", maxLength: 1000, nullable: true)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_investigations", x => x.Id);
                    table.ForeignKey(
                        name: "FK_investigations_incidents_IncidentId",
                        column: x => x.IncidentId,
                        principalSchema: "sentinel",
                        principalTable: "incidents",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "postmortems",
                schema: "sentinel",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    IncidentId = table.Column<Guid>(type: "uuid", nullable: false),
                    ContentMarkdown = table.Column<string>(type: "text", nullable: false),
                    Structured = table.Column<string>(type: "jsonb", nullable: true),
                    DurationMinutes = table.Column<int>(type: "integer", nullable: true),
                    LessonsLearned = table.Column<string>(type: "character varying(4000)", maxLength: 4000, nullable: true),
                    ChangedFiles = table.Column<string>(type: "jsonb", nullable: true),
                    RelevantCommits = table.Column<string>(type: "jsonb", nullable: true),
                    IngestedToRag = table.Column<bool>(type: "boolean", nullable: false),
                    CreatedAt = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_postmortems", x => x.Id);
                    table.ForeignKey(
                        name: "FK_postmortems_incidents_IncidentId",
                        column: x => x.IncidentId,
                        principalSchema: "sentinel",
                        principalTable: "incidents",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "hypotheses",
                schema: "sentinel",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    InvestigationId = table.Column<Guid>(type: "uuid", nullable: false),
                    Title = table.Column<string>(type: "character varying(300)", maxLength: 300, nullable: false),
                    Description = table.Column<string>(type: "character varying(4000)", maxLength: 4000, nullable: true),
                    Score = table.Column<decimal>(type: "numeric(3,2)", precision: 3, scale: 2, nullable: false),
                    Rank = table.Column<int>(type: "integer", nullable: false),
                    IsSelected = table.Column<bool>(type: "boolean", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_hypotheses", x => x.Id);
                    table.ForeignKey(
                        name: "FK_hypotheses_investigations_InvestigationId",
                        column: x => x.InvestigationId,
                        principalSchema: "sentinel",
                        principalTable: "investigations",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "investigation_steps",
                schema: "sentinel",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    InvestigationId = table.Column<Guid>(type: "uuid", nullable: false),
                    Sequence = table.Column<int>(type: "integer", nullable: false),
                    State = table.Column<string>(type: "character varying(64)", maxLength: 64, nullable: false),
                    Message = table.Column<string>(type: "character varying(2000)", maxLength: 2000, nullable: false),
                    Payload = table.Column<string>(type: "jsonb", nullable: true),
                    DurationMs = table.Column<int>(type: "integer", nullable: true),
                    StartedAt = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false),
                    CompletedAt = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: true)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_investigation_steps", x => x.Id);
                    table.ForeignKey(
                        name: "FK_investigation_steps_investigations_InvestigationId",
                        column: x => x.InvestigationId,
                        principalSchema: "sentinel",
                        principalTable: "investigations",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "model_predictions",
                schema: "sentinel",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    InvestigationId = table.Column<Guid>(type: "uuid", nullable: true),
                    ModelName = table.Column<string>(type: "character varying(100)", maxLength: 100, nullable: false),
                    Purpose = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    PromptTokens = table.Column<int>(type: "integer", nullable: false),
                    CompletionTokens = table.Column<int>(type: "integer", nullable: false),
                    LatencyMs = table.Column<int>(type: "integer", nullable: false),
                    ValidJson = table.Column<bool>(type: "boolean", nullable: false),
                    Output = table.Column<string>(type: "jsonb", nullable: true),
                    CalledAt = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_model_predictions", x => x.Id);
                    table.ForeignKey(
                        name: "FK_model_predictions_investigations_InvestigationId",
                        column: x => x.InvestigationId,
                        principalSchema: "sentinel",
                        principalTable: "investigations",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.SetNull);
                });

            migrationBuilder.CreateTable(
                name: "root_causes",
                schema: "sentinel",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    InvestigationId = table.Column<Guid>(type: "uuid", nullable: false),
                    HypothesisId = table.Column<Guid>(type: "uuid", nullable: true),
                    Title = table.Column<string>(type: "character varying(300)", maxLength: 300, nullable: false),
                    Category = table.Column<string>(type: "character varying(64)", maxLength: 64, nullable: false),
                    Confidence = table.Column<decimal>(type: "numeric(3,2)", precision: 3, scale: 2, nullable: false),
                    Explanation = table.Column<string>(type: "character varying(8000)", maxLength: 8000, nullable: true),
                    ValidatorOutput = table.Column<string>(type: "jsonb", nullable: true),
                    ValidatorConfidence = table.Column<decimal>(type: "numeric(3,2)", precision: 3, scale: 2, nullable: true)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_root_causes", x => x.Id);
                    table.ForeignKey(
                        name: "FK_root_causes_hypotheses_HypothesisId",
                        column: x => x.HypothesisId,
                        principalSchema: "sentinel",
                        principalTable: "hypotheses",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.SetNull);
                    table.ForeignKey(
                        name: "FK_root_causes_investigations_InvestigationId",
                        column: x => x.InvestigationId,
                        principalSchema: "sentinel",
                        principalTable: "investigations",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "evidence",
                schema: "sentinel",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    InvestigationId = table.Column<Guid>(type: "uuid", nullable: false),
                    StepId = table.Column<Guid>(type: "uuid", nullable: true),
                    Source = table.Column<string>(type: "character varying(30)", maxLength: 30, nullable: false),
                    Summary = table.Column<string>(type: "character varying(2000)", maxLength: 2000, nullable: false),
                    Raw = table.Column<string>(type: "jsonb", nullable: true),
                    Weight = table.Column<decimal>(type: "numeric(3,2)", precision: 3, scale: 2, nullable: false),
                    CreatedAt = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_evidence", x => x.Id);
                    table.ForeignKey(
                        name: "FK_evidence_investigation_steps_StepId",
                        column: x => x.StepId,
                        principalSchema: "sentinel",
                        principalTable: "investigation_steps",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.SetNull);
                    table.ForeignKey(
                        name: "FK_evidence_investigations_InvestigationId",
                        column: x => x.InvestigationId,
                        principalSchema: "sentinel",
                        principalTable: "investigations",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "recommendations",
                schema: "sentinel",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    InvestigationId = table.Column<Guid>(type: "uuid", nullable: false),
                    RootCauseId = table.Column<Guid>(type: "uuid", nullable: false),
                    ActionCode = table.Column<string>(type: "character varying(64)", maxLength: 64, nullable: false),
                    Description = table.Column<string>(type: "character varying(2000)", maxLength: 2000, nullable: false),
                    ToolName = table.Column<string>(type: "character varying(100)", maxLength: 100, nullable: true),
                    ToolArgs = table.Column<string>(type: "jsonb", nullable: true),
                    RequiresApproval = table.Column<bool>(type: "boolean", nullable: false),
                    Status = table.Column<string>(type: "character varying(30)", maxLength: 30, nullable: false),
                    ApprovedBy = table.Column<Guid>(type: "uuid", nullable: true),
                    ApprovedAt = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: true),
                    ExecutedAt = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: true),
                    ExecutionResult = table.Column<string>(type: "jsonb", nullable: true),
                    VerificationResult = table.Column<string>(type: "jsonb", nullable: true)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_recommendations", x => x.Id);
                    table.ForeignKey(
                        name: "FK_recommendations_investigations_InvestigationId",
                        column: x => x.InvestigationId,
                        principalSchema: "sentinel",
                        principalTable: "investigations",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                    table.ForeignKey(
                        name: "FK_recommendations_root_causes_RootCauseId",
                        column: x => x.RootCauseId,
                        principalSchema: "sentinel",
                        principalTable: "root_causes",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                    table.ForeignKey(
                        name: "FK_recommendations_users_ApprovedBy",
                        column: x => x.ApprovedBy,
                        principalSchema: "sentinel",
                        principalTable: "users",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.SetNull);
                });

            migrationBuilder.CreateTable(
                name: "tool_calls",
                schema: "sentinel",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    InvestigationId = table.Column<Guid>(type: "uuid", nullable: false),
                    StepId = table.Column<Guid>(type: "uuid", nullable: true),
                    Server = table.Column<string>(type: "character varying(50)", maxLength: 50, nullable: false),
                    Tool = table.Column<string>(type: "character varying(100)", maxLength: 100, nullable: false),
                    Args = table.Column<string>(type: "jsonb", nullable: true),
                    ResultSummary = table.Column<string>(type: "character varying(4000)", maxLength: 4000, nullable: true),
                    Success = table.Column<bool>(type: "boolean", nullable: false),
                    LatencyMs = table.Column<int>(type: "integer", nullable: false),
                    IsDestructive = table.Column<bool>(type: "boolean", nullable: false),
                    ApprovalId = table.Column<Guid>(type: "uuid", nullable: true),
                    CalledAt = table.Column<DateTimeOffset>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_tool_calls", x => x.Id);
                    table.ForeignKey(
                        name: "FK_tool_calls_investigation_steps_StepId",
                        column: x => x.StepId,
                        principalSchema: "sentinel",
                        principalTable: "investigation_steps",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.SetNull);
                    table.ForeignKey(
                        name: "FK_tool_calls_investigations_InvestigationId",
                        column: x => x.InvestigationId,
                        principalSchema: "sentinel",
                        principalTable: "investigations",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                    table.ForeignKey(
                        name: "FK_tool_calls_recommendations_ApprovalId",
                        column: x => x.ApprovalId,
                        principalSchema: "sentinel",
                        principalTable: "recommendations",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.SetNull);
                });

            migrationBuilder.CreateIndex(
                name: "IX_evaluation_results_RunId_Passed",
                schema: "sentinel",
                table: "evaluation_results",
                columns: new[] { "RunId", "Passed" });

            migrationBuilder.CreateIndex(
                name: "IX_evaluation_runs_Kind_StartedAt",
                schema: "sentinel",
                table: "evaluation_runs",
                columns: new[] { "Kind", "StartedAt" });

            migrationBuilder.CreateIndex(
                name: "IX_evidence_InvestigationId",
                schema: "sentinel",
                table: "evidence",
                column: "InvestigationId");

            migrationBuilder.CreateIndex(
                name: "IX_evidence_StepId",
                schema: "sentinel",
                table: "evidence",
                column: "StepId");

            migrationBuilder.CreateIndex(
                name: "IX_hypotheses_InvestigationId_Rank",
                schema: "sentinel",
                table: "hypotheses",
                columns: new[] { "InvestigationId", "Rank" });

            migrationBuilder.CreateIndex(
                name: "IX_incidents_CreatedBy",
                schema: "sentinel",
                table: "incidents",
                column: "CreatedBy");

            migrationBuilder.CreateIndex(
                name: "IX_incidents_IncidentCode",
                schema: "sentinel",
                table: "incidents",
                column: "IncidentCode",
                unique: true);

            migrationBuilder.CreateIndex(
                name: "IX_incidents_ServiceId_Status",
                schema: "sentinel",
                table: "incidents",
                columns: new[] { "ServiceId", "Status" });

            migrationBuilder.CreateIndex(
                name: "IX_incidents_StartedAt",
                schema: "sentinel",
                table: "incidents",
                column: "StartedAt");

            migrationBuilder.CreateIndex(
                name: "IX_investigation_steps_InvestigationId_Sequence",
                schema: "sentinel",
                table: "investigation_steps",
                columns: new[] { "InvestigationId", "Sequence" },
                unique: true);

            migrationBuilder.CreateIndex(
                name: "IX_investigations_IncidentId",
                schema: "sentinel",
                table: "investigations",
                column: "IncidentId");

            migrationBuilder.CreateIndex(
                name: "IX_investigations_Status",
                schema: "sentinel",
                table: "investigations",
                column: "Status");

            migrationBuilder.CreateIndex(
                name: "IX_model_predictions_InvestigationId",
                schema: "sentinel",
                table: "model_predictions",
                column: "InvestigationId");

            migrationBuilder.CreateIndex(
                name: "IX_model_predictions_ModelName_Purpose_CalledAt",
                schema: "sentinel",
                table: "model_predictions",
                columns: new[] { "ModelName", "Purpose", "CalledAt" });

            migrationBuilder.CreateIndex(
                name: "IX_postmortems_IncidentId",
                schema: "sentinel",
                table: "postmortems",
                column: "IncidentId");

            migrationBuilder.CreateIndex(
                name: "IX_postmortems_IngestedToRag",
                schema: "sentinel",
                table: "postmortems",
                column: "IngestedToRag");

            migrationBuilder.CreateIndex(
                name: "IX_recommendations_ApprovedBy",
                schema: "sentinel",
                table: "recommendations",
                column: "ApprovedBy");

            migrationBuilder.CreateIndex(
                name: "IX_recommendations_InvestigationId",
                schema: "sentinel",
                table: "recommendations",
                column: "InvestigationId");

            migrationBuilder.CreateIndex(
                name: "IX_recommendations_RootCauseId",
                schema: "sentinel",
                table: "recommendations",
                column: "RootCauseId");

            migrationBuilder.CreateIndex(
                name: "IX_recommendations_Status",
                schema: "sentinel",
                table: "recommendations",
                column: "Status");

            migrationBuilder.CreateIndex(
                name: "IX_root_causes_Category",
                schema: "sentinel",
                table: "root_causes",
                column: "Category");

            migrationBuilder.CreateIndex(
                name: "IX_root_causes_HypothesisId",
                schema: "sentinel",
                table: "root_causes",
                column: "HypothesisId");

            migrationBuilder.CreateIndex(
                name: "IX_root_causes_InvestigationId",
                schema: "sentinel",
                table: "root_causes",
                column: "InvestigationId");

            migrationBuilder.CreateIndex(
                name: "IX_services_Name",
                schema: "sentinel",
                table: "services",
                column: "Name",
                unique: true);

            migrationBuilder.CreateIndex(
                name: "IX_tool_calls_ApprovalId",
                schema: "sentinel",
                table: "tool_calls",
                column: "ApprovalId");

            migrationBuilder.CreateIndex(
                name: "IX_tool_calls_InvestigationId",
                schema: "sentinel",
                table: "tool_calls",
                column: "InvestigationId");

            migrationBuilder.CreateIndex(
                name: "IX_tool_calls_IsDestructive",
                schema: "sentinel",
                table: "tool_calls",
                column: "IsDestructive",
                filter: "is_destructive = true");

            migrationBuilder.CreateIndex(
                name: "IX_tool_calls_StepId",
                schema: "sentinel",
                table: "tool_calls",
                column: "StepId");

            migrationBuilder.CreateIndex(
                name: "IX_tool_calls_Tool_CalledAt",
                schema: "sentinel",
                table: "tool_calls",
                columns: new[] { "Tool", "CalledAt" });

            migrationBuilder.CreateIndex(
                name: "IX_users_Username",
                schema: "sentinel",
                table: "users",
                column: "Username",
                unique: true);
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropTable(
                name: "evaluation_results",
                schema: "sentinel");

            migrationBuilder.DropTable(
                name: "evidence",
                schema: "sentinel");

            migrationBuilder.DropTable(
                name: "model_predictions",
                schema: "sentinel");

            migrationBuilder.DropTable(
                name: "postmortems",
                schema: "sentinel");

            migrationBuilder.DropTable(
                name: "tool_calls",
                schema: "sentinel");

            migrationBuilder.DropTable(
                name: "evaluation_runs",
                schema: "sentinel");

            migrationBuilder.DropTable(
                name: "investigation_steps",
                schema: "sentinel");

            migrationBuilder.DropTable(
                name: "recommendations",
                schema: "sentinel");

            migrationBuilder.DropTable(
                name: "root_causes",
                schema: "sentinel");

            migrationBuilder.DropTable(
                name: "hypotheses",
                schema: "sentinel");

            migrationBuilder.DropTable(
                name: "investigations",
                schema: "sentinel");

            migrationBuilder.DropTable(
                name: "incidents",
                schema: "sentinel");

            migrationBuilder.DropTable(
                name: "services",
                schema: "sentinel");

            migrationBuilder.DropTable(
                name: "users",
                schema: "sentinel");
        }
    }
}
