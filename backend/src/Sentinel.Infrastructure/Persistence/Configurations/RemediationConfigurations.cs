using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Metadata.Builders;
using Sentinel.Domain.Entities;

namespace Sentinel.Infrastructure.Persistence.Configurations;

public sealed class RecommendationConfiguration : IEntityTypeConfiguration<Recommendation>
{
    public void Configure(EntityTypeBuilder<Recommendation> builder)
    {
        builder.ToTable("recommendations");
        builder.HasKey(r => r.Id);

        builder.Property(r => r.ActionCode).HasMaxLength(64).IsRequired();
        builder.Property(r => r.Description).HasMaxLength(2000).IsRequired();
        builder.Property(r => r.ToolName).HasMaxLength(100);
        builder.Property(r => r.Status).HasConversion<string>().HasMaxLength(30);

        builder.Property(r => r.ToolArgs).HasColumnType("jsonb");
        builder.Property(r => r.ExecutionResult).HasColumnType("jsonb");
        builder.Property(r => r.VerificationResult).HasColumnType("jsonb");

        builder.HasIndex(r => r.InvestigationId);
        builder.HasIndex(r => r.Status);

        builder.HasOne(r => r.Investigation)
            .WithMany()
            .HasForeignKey(r => r.InvestigationId)
            .OnDelete(DeleteBehavior.Cascade);

        builder.HasOne(r => r.RootCause)
            .WithMany(rc => rc.Recommendations)
            .HasForeignKey(r => r.RootCauseId)
            .OnDelete(DeleteBehavior.Cascade);

        // Approvals are an audit trail. Deleting the user who approved must not erase the record
        // that an approval happened, so the reference is nulled rather than cascaded.
        builder.HasOne(r => r.Approver)
            .WithMany()
            .HasForeignKey(r => r.ApprovedBy)
            .OnDelete(DeleteBehavior.SetNull);
    }
}

public sealed class PostmortemConfiguration : IEntityTypeConfiguration<Postmortem>
{
    public void Configure(EntityTypeBuilder<Postmortem> builder)
    {
        builder.ToTable("postmortems");
        builder.HasKey(p => p.Id);

        builder.Property(p => p.ContentMarkdown).IsRequired();
        builder.Property(p => p.LessonsLearned).HasMaxLength(4000);

        builder.Property(p => p.Structured).HasColumnType("jsonb");
        builder.Property(p => p.ChangedFiles).HasColumnType("jsonb");
        builder.Property(p => p.RelevantCommits).HasColumnType("jsonb");

        builder.HasIndex(p => p.IncidentId);

        // Lets the ingest job find postmortems that never made it into the chunk store.
        builder.HasIndex(p => p.IngestedToRag);

        builder.HasOne(p => p.Incident)
            .WithMany(i => i.Postmortems)
            .HasForeignKey(p => p.IncidentId)
            .OnDelete(DeleteBehavior.Cascade);
    }
}
