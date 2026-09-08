using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Metadata.Builders;
using Sentinel.Domain.Entities;

namespace Sentinel.Infrastructure.Persistence.Configurations;

public sealed class ServiceConfiguration : IEntityTypeConfiguration<Service>
{
    public void Configure(EntityTypeBuilder<Service> builder)
    {
        builder.ToTable("services");
        builder.HasKey(s => s.Id);

        builder.Property(s => s.Name).HasMaxLength(100).IsRequired();
        builder.Property(s => s.DisplayName).HasMaxLength(200).IsRequired();
        builder.Property(s => s.RepoPath).HasMaxLength(500);
        builder.Property(s => s.HealthUrl).HasMaxLength(500);
        builder.Property(s => s.MetricsJob).HasMaxLength(100);

        builder.HasIndex(s => s.Name).IsUnique();
    }
}

public sealed class UserConfiguration : IEntityTypeConfiguration<User>
{
    public void Configure(EntityTypeBuilder<User> builder)
    {
        builder.ToTable("users");
        builder.HasKey(u => u.Id);

        builder.Property(u => u.Username).HasMaxLength(100).IsRequired();
        builder.Property(u => u.PasswordHash).HasMaxLength(200).IsRequired();
        builder.Property(u => u.Role).HasConversion<string>().HasMaxLength(20);

        builder.HasIndex(u => u.Username).IsUnique();
    }
}

public sealed class IncidentConfiguration : IEntityTypeConfiguration<Incident>
{
    public void Configure(EntityTypeBuilder<Incident> builder)
    {
        builder.ToTable("incidents");
        builder.HasKey(i => i.Id);

        // Generated in the database rather than the application, so two concurrent inserts
        // cannot produce the same code.
        builder.Property(i => i.IncidentCode)
            .HasMaxLength(20)
            .IsRequired()
            .HasDefaultValueSql("sentinel.next_incident_code()")
            .ValueGeneratedOnAdd();

        builder.Property(i => i.Title).HasMaxLength(300).IsRequired();
        builder.Property(i => i.Description).HasMaxLength(4000);
        builder.Property(i => i.Severity).HasConversion<string>().HasMaxLength(20);
        builder.Property(i => i.Status).HasConversion<string>().HasMaxLength(30);

        builder.HasIndex(i => i.IncidentCode).IsUnique();

        // The dashboard's main query: open incidents for a service.
        builder.HasIndex(i => new { i.ServiceId, i.Status });
        builder.HasIndex(i => i.StartedAt);

        builder.HasOne(i => i.Service)
            .WithMany(s => s.Incidents)
            .HasForeignKey(i => i.ServiceId)
            .OnDelete(DeleteBehavior.Restrict);

        builder.HasOne(i => i.Creator)
            .WithMany()
            .HasForeignKey(i => i.CreatedBy)
            .OnDelete(DeleteBehavior.SetNull);
    }
}
